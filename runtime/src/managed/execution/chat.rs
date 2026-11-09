//! The chat source and the chat run's bookkeeping (N2b-2 C2; design §4.1,
//! §3.7; ADR-034 §5): desktop's `watch_chat` and `start_or_inject` on owned
//! threads. Shared with desktop, unchanged: the answered-cut
//! (`snapshot_backlog`), `records_in`, `resolve_reply`, the watcher's dedup
//! and routing, the follow-ups (`post_credential_requests`, `follow_ups`,
//! `follow_up_body`) and the interrupted turn (`next_turn_seq`,
//! `interrupted_turn_record`). Managed-only, structurally:
//!
//! - the watch connects only while open and owning the responder, and ends
//!   at its next frame on any close (the generation it subscribed in), the
//!   form desktop's per-frame `answers_chat` takes here; every handler
//!   re-checks that generation under `state` before it acts (D20);
//! - a new conversation takes the chat lane's permit and is offered to the
//!   lane, instead of a thread per conversation; a message for the live run
//!   is injected under the live-run lock, admitted only while the generation
//!   it arrived in is current and the service open (desktop parity, R4);
//! - a run offered but not live yet (in its lane's inbox behind the previous
//!   report, or in its take→register gap) cannot take an inject: its inputs
//!   wait, in order, until it is (`flush_parked`), or for its report, which
//!   re-dispatches them — desktop registers the run's mailbox before it
//!   starts, so nothing typed meanwhile is lost there either;
//! - the interrupted turn goes to the setup-recorded `turns` address (the
//!   gate admits no runtime discovery of the log child).
//!
//! Desktop parity by the user's ruling (design §10 R1, R2): no post-time
//! fence — a run that a revoking close let finish posts its follow-ups as on
//! desktop — and nothing durable marks a message handed to a run.
use super::*;
use crate::anyapi::AnyError;
use crate::chatwatch::{
    follow_up_body, follow_ups, interrupted_turn_record, next_turn_seq, post_credential_requests,
    preview, records_in, resolve_reply, snapshot_backlog, FollowUp,
};
use crate::managed::lanes::{inject_if, soft_break_live, MailboxError};
use crate::triggers::{control_break, ChatInput, WatchAction, CHAT_MESSAGES, CHAT_WATCH_ID};

/// The conversation program (desktop `serve::CHAT_PROGRAM`).
pub(crate) const CHAT_PROGRAM: &str = "agent:toolcaller@v1";

/// The conversation run in flight, from its offer until its report took it
/// down. Its interrupt flag is the job's own: a hard break reaches it whether
/// it is live yet or not.
pub(super) struct ChatRun {
    pub(super) run_id: String,
    pub(super) interrupt: Arc<AtomicBool>,
}

/// An input that waits for the chat run to be live.
pub(super) enum Parked {
    /// A message (`{kind: inject, …}`, desktop's mailbox item).
    Inject(Value),
    /// A soft break (ADR-005 §3).
    SoftBreak,
}

impl Inner {
    // --- the chat watch (`Task::ChatWatch`) ------------------------------------------

    /// Desktop's watch thread: connected iff open and owning the enabled
    /// responder record (ADR-018 §3); a feed error or a stream that ended
    /// reconnects after the pause, with a fresh snapshot.
    pub(super) fn chat_loop(&self, ctx: &TaskContext) {
        while !self.cancelled() {
            if !self.open.load(Ordering::SeqCst) || !self.owns_chat.load(Ordering::SeqCst) {
                ctx.idle_until(Instant::now() + self.config.cadence.wake);
                continue;
            }
            let g = self.gen.load(Ordering::SeqCst);
            if let Err(e) = self.watch_chat(g) {
                warn!("chat watch: feed error: {e}; reconnecting");
            }
            ctx.idle_until(Instant::now() + self.config.cadence.reconnect);
        }
    }

    /// Still the stream the generation `g` subscribed: open, owning the
    /// responder, no close since.
    fn chat_connected(&self, g: u64) -> bool {
        !self.cancelled()
            && self.gen.load(Ordering::SeqCst) == g
            && self.open.load(Ordering::SeqCst)
            && self.owns_chat.load(Ordering::SeqCst)
    }

    /// One subscription (desktop `watch_chat`): the snapshot's backlog — user
    /// messages newer than this agent's last terminal reply — and every
    /// message of a `changes` frame, in order.
    fn watch_chat(&self, g: u64) -> Result<(), AnyError> {
        let frames = self.client.subscribe_dataset(
            &self.config.space,
            &self.config.chat,
            CHAT_MESSAGES,
            &json!({"sort": ["-createdAt"], "limit": 64}),
        )?;
        for frame in frames {
            if !self.chat_connected(g) {
                return Ok(());
            }
            match frame.event.as_str() {
                "ready" => continue,
                "closed" => return Ok(()),
                "snapshot" => {
                    for record in snapshot_backlog(&frame.data, &self.config.agent_name) {
                        self.on_chat_message(g, record, true);
                    }
                }
                "changes" => {
                    for record in records_in(&frame.data) {
                        self.on_chat_message(g, record, false);
                    }
                }
                other => {
                    warn!("chat watch: unexpected frame {other:?}; reconnecting");
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    /// One message: its reply referent resolved outside `state` (desktop's
    /// order), then — under `state`, while the generation it arrived in is
    /// current — the watcher's verdict. The sender is not folded: the
    /// members and identity routes are not host routes, and the PoC
    /// account's one member makes the fold a no-op (BOB-192).
    fn on_chat_message(&self, g: u64, record: Value, backlog: bool) {
        let record = resolve_reply(&self.client, &self.config.space, &self.config.chat, record);
        let note = {
            let mut st = self.lock();
            // a close since the frame: dropped here, re-derived by the
            // reopen's snapshot (the close reset the watcher)
            if self.gen.load(Ordering::SeqCst) != g || !self.open.load(Ordering::SeqCst) {
                return;
            }
            let chat = self.config.chat.clone();
            match st.watcher.on_message(&chat, &record) {
                WatchAction::Start => {
                    let input = Watcher::input(&record);
                    if backlog {
                        info!("backlog conversation: {:?}", preview(&input.text));
                    } else {
                        info!("conversation started: {:?}", preview(&input.text));
                    }
                    self.start_or_inject(&mut st, g, input, true)
                }
                WatchAction::BreakIdle => {
                    self.chat_break(&mut st, g, control_break(&record).unwrap_or(false));
                    None
                }
                // `live` stays empty, so the watcher never injects or breaks
                // itself; duplicates and own bubbles are nothing
                WatchAction::Dup
                | WatchAction::Skip
                | WatchAction::Inject
                | WatchAction::Break { .. } => None,
            }
        };
        if let Some(note) = note {
            self.persist_chat_note(note);
        }
    }

    /// Desktop `start_or_inject`, under `state`: into the live run's mailbox,
    /// else — behind inputs still parked, or for a run not live yet — parked;
    /// with no run, a new one under the chat lane's permit. With `note`, a
    /// started conversation is counted on the responder (desktop
    /// `note_chat_start`, on the watch thread): the record to persist after
    /// `state` is released comes back.
    pub(super) fn start_or_inject(
        &self,
        st: &mut State,
        g: u64,
        input: ChatInput,
        note: bool,
    ) -> Option<(String, Value)> {
        let item = json!({"kind": "inject", "text": input.text, "context": input.context,
                          "messageId": input.message_id});
        if let Some(run) = st.chat.as_ref().map(|c| c.run_id.clone()) {
            if st.parked.is_empty() {
                let admit =
                    || self.gen.load(Ordering::SeqCst) == g && self.open.load(Ordering::SeqCst);
                match inject_if(&self.shared, &run, item.clone(), admit) {
                    Ok(()) => return None,
                    // counted where the bound is enforced (`push_live`)
                    Err(MailboxError::Full) => {
                        warn!(
                            "chat run {run}: mailbox full, message {:?} not injected (a capacity \
                             fault)",
                            input.message_id
                        );
                        return None;
                    }
                    Err(MailboxError::Refused) => return None,
                    Err(MailboxError::NotLive) => {}
                }
            }
            self.park(st, g, Parked::Inject(item));
            return None;
        }
        // admission (ADR-034 §7): a fresh grant and the lane's permit; a
        // closed gate is a close under way, whose reset watcher re-derives
        // the message from the next snapshot
        if !st.grant_fresh() {
            info!(
                "chat: the grant is not fresh, message {:?} not started",
                input.message_id
            );
            return None;
        }
        let permit = match self.shared.pool.acquire(Lane::Chat) {
            Admission::Admitted(permit) => permit,
            Admission::CapacityDeferred => {
                info!(
                    "chat: admission closed, message {:?} not started",
                    input.message_id
                );
                return None;
            }
        };
        self.start_chat(st, permit, input, note)
    }

    /// A new conversation run (desktop's args, `:start_or_inject`), offered
    /// to the chat lane; it counts toward quiescence from the offer.
    fn start_chat(
        &self,
        st: &mut State,
        permit: StorePermit,
        input: ChatInput,
        note: bool,
    ) -> Option<(String, Value)> {
        let lanes = self.lanes.get()?;
        let responder = owned_chat_watch(&st.registry, &self.config.self_peer);
        let run_id = new_run_id();
        let c = &self.config;
        let args = json!({
            "space": c.space, "chatId": c.chat, "userText": input.text,
            "uiContext": input.context, "agentName": c.agent_name, "traceRef": run_id,
            "codeSpace": c.code_space, "overlays": c.overlays});
        let interrupt = Arc::new(AtomicBool::new(false));
        let job = LaneJob {
            lane: Lane::Chat,
            work_id: responder
                .clone()
                .unwrap_or_else(|| CHAT_WATCH_ID.to_owned()),
            run_id: run_id.clone(),
            spec: CHAT_PROGRAM.to_owned(),
            args,
            trigger: responder.clone(),
            mailbox: Default::default(),
            interrupt: interrupt.clone(),
        };
        st.jobs.insert(
            run_id.clone(),
            JobKind::Chat {
                user_text: input.text.clone(),
                responder: responder.clone(),
            },
        );
        st.chat = Some(ChatRun {
            run_id: run_id.clone(),
            interrupt,
        });
        self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        if let Err(refused) = lanes.offer(job, permit, &self.shared) {
            // the pool closed between `acquire` and `offer` (D13): never
            // started; the close's reset watcher re-derives the message
            drop(refused);
            info!("chat: offer refused (stopping), message not started");
            st.jobs.remove(&run_id);
            st.chat = None;
            self.inflight.store(st.jobs.len(), Ordering::SeqCst);
            return None;
        }
        if !note {
            return None;
        }
        // desktop `note_chat_start`: a conversation start counts as one run
        // on the responder's rollup
        let id = responder?;
        let t = st.registry.get_mut(&id)?;
        t.last_run_at = Some((self.now)());
        t.last_status = Some("ok".into());
        t.consecutive_failures = 0;
        self.chat_notes.fetch_add(1, Ordering::SeqCst);
        Some((id, persisted_record(t)))
    }

    /// The conversation-start record, written outside `state` (desktop
    /// ignores its result); the stand-down write waits for it.
    fn persist_chat_note(&self, (id, record): (String, Value)) {
        let triggers = &self.config.triggers;
        if let Err(e) = self.client.upsert_record(
            &self.config.space,
            &triggers.object_id,
            &triggers.collection,
            &id,
            &record,
        ) {
            debug!("chat responder {id:?}: conversation start not persisted: {e}");
        }
        self.chat_notes.fetch_sub(1, Ordering::SeqCst);
        self.publish();
        self.standdown_kick.store(true, Ordering::SeqCst);
        self.shared.pool.notify();
    }

    /// A `break` control record (ADR-005 §3). Hard: the run's own flag, live
    /// or not yet. Soft: the break item into its mailbox and the grace
    /// deadline on the dispatcher — parked while the run is not live. With no
    /// run there is nothing to stop, and no deferred backlog to drop.
    fn chat_break(&self, st: &mut State, g: u64, hard: bool) {
        let Some(run) = st.chat.as_ref() else {
            return;
        };
        let chat = &self.config.chat;
        if hard {
            info!("hard break on chat {chat}");
            run.interrupt.store(true, Ordering::SeqCst);
            return;
        }
        let run_id = run.run_id.clone();
        if st.parked.is_empty() {
            match soft_break_live(&self.shared, &run_id) {
                Ok(()) => {
                    self.armed_soft_break(st, run_id);
                    return;
                }
                Err(MailboxError::NotLive) => {}
                Err(_) => return,
            }
        }
        self.park(st, g, Parked::SoftBreak);
    }

    fn armed_soft_break(&self, st: &mut State, run_id: String) {
        let grace = self.config.cadence.break_grace;
        info!(
            "soft break on chat {} (hard in {}s unless acknowledged)",
            self.config.chat,
            grace.as_secs()
        );
        st.breaks.push((Instant::now() + grace, run_id));
    }

    /// Bounded like the deferred chat backlog (ADR-034 §2): a full one is a
    /// counted capacity fault, the input dropped visibly.
    fn park(&self, st: &mut State, g: u64, item: Parked) {
        if st.parked.len() >= MAX_BACKLOG {
            self.shared.capacity_fault();
            warn!("chat: {MAX_BACKLOG} inputs wait for a run not live yet; input dropped (a capacity fault)");
            return;
        }
        st.parked.push_back((g, item));
    }

    /// The dispatcher's pass: parked inputs into the chat run once it is
    /// live, in order; a close since an input arrived drops it.
    pub(super) fn flush_parked(&self) {
        let mut st = self.lock();
        let Some(run) = st.chat.as_ref().map(|c| c.run_id.clone()) else {
            return;
        };
        while let Some((g, item)) = st.parked.pop_front() {
            if g != self.gen.load(Ordering::SeqCst) || !self.open.load(Ordering::SeqCst) {
                continue;
            }
            let result = match &item {
                Parked::Inject(value) => inject_if(&self.shared, &run, value.clone(), || true),
                Parked::SoftBreak => soft_break_live(&self.shared, &run),
            };
            match result {
                Ok(()) => {
                    if let Parked::SoftBreak = item {
                        self.armed_soft_break(&mut st, run.clone());
                    }
                }
                Err(MailboxError::NotLive) => {
                    st.parked.push_front((g, item));
                    return;
                }
                // a full mailbox is counted where it is refused
                Err(_) => {}
            }
        }
    }

    // --- the chat run's report (on the lane thread) ----------------------------------

    /// Desktop `start_or_inject`'s tail, in its order: the run taken down
    /// first (a message typed while the bubbles post starts a fresh run),
    /// then — for a run that produced a result — the credential cards and at
    /// most one follow-up, the interrupted turn after "Stopped.", then the
    /// undrained input re-dispatched. A run that produced no result (a run
    /// path failure, an unlanded trace, or one cancelled before it started)
    /// posts nothing, as desktop's `ctx.run` error does.
    pub(super) fn finish_chat(
        &self,
        report: &LaneReport,
        user_text: &str,
        responder: Option<&str>,
        mut leftovers: Vec<Value>,
    ) {
        {
            let mut st = self.lock();
            if st.chat.as_ref().is_some_and(|c| c.run_id == report.run_id) {
                st.chat = None;
                for (_, item) in std::mem::take(&mut st.parked) {
                    if let Parked::Inject(value) = item {
                        leftovers.push(value);
                    }
                }
            }
        }
        leftovers.extend(report.refused_input.iter().cloned());
        if let Ok(rr) = &report.outcome.result {
            self.chat_follow_ups(report, rr, user_text, responder);
        }
        let inputs: Vec<ChatInput> = leftovers
            .into_iter()
            .filter(|item| item["kind"] == "inject")
            .map(|item| ChatInput {
                text: item["text"].as_str().unwrap_or_default().to_owned(),
                context: item["context"].clone(),
                message_id: item["messageId"].as_str().unwrap_or_default().to_owned(),
                deferrals: 0,
            })
            .collect();
        if inputs.is_empty() {
            return;
        }
        let mut st = self.lock();
        if !self.open.load(Ordering::SeqCst) {
            info!(
                "chat: {} undrained input(s) not re-dispatched (closed); the reopen's snapshot \
                 re-derives them",
                inputs.len()
            );
            return;
        }
        let g = self.gen.load(Ordering::SeqCst);
        for input in inputs {
            self.start_or_inject(&mut st, g, input, false);
        }
    }

    fn chat_follow_ups(
        &self,
        report: &LaneReport,
        rr: &RunResult,
        user_text: &str,
        responder: Option<&str>,
    ) {
        let (c, space) = (&self.config, &self.config.space);
        let trace_ref = report.run_id.as_str();
        if let Some(summary) = report.outcome.summary.clone() {
            if let Err(e) = publish_run_summary(
                &self.client,
                space,
                &c.runs.object_id,
                &c.runs.collection,
                Some(&c.self_peer),
                summary,
                responder,
            ) {
                warn!("agent_runs {trace_ref}: summary not published: {e}");
            }
        }
        let posted = post_credential_requests(
            &self.client,
            space,
            &c.chat,
            &c.secrets.object_id,
            &c.secrets.collection,
            self.config_store.as_deref(),
            &c.agent_name,
            &rr.missing_secrets,
            &|warning| warn!("{warning}"),
        );
        // the frozen resolver reports no program miss, so nothing is
        // deferred on one (design §2)
        for follow_up in follow_ups(rr, posted, 0) {
            let _ = self.client.chat_send(
                space,
                &c.chat,
                &follow_up_body(&follow_up, &c.agent_name, trace_ref),
            );
            if let FollowUp::Stopped = follow_up {
                if let Err(e) = self.append_interrupted_turn(user_text, trace_ref) {
                    warn!("interrupted turn not logged (trace {trace_ref}): {e}");
                }
            }
        }
    }

    /// The turn the trapped guest could not log (ADR-005 §3), on the
    /// setup-recorded log child: desktop's seq probe and record.
    fn append_interrupted_turn(&self, user_text: &str, trace_ref: &str) -> Result<(), AnyError> {
        let (space, turns) = (&self.config.space, &self.config.turns);
        let seq = next_turn_seq(&self.client, space, &turns.object_id, &turns.collection)?;
        self.client.upsert_record(
            space,
            &turns.object_id,
            &turns.collection,
            &format!("{seq:08}"),
            &interrupted_turn_record(seq, &self.config.agent_name, user_text, trace_ref),
        )?;
        Ok(())
    }
}
