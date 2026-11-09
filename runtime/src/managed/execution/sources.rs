//! Event sources (N2b-2 C3; design §4.2; ADR-018 §2, ADR-034 §2/§5):
//! desktop's `reconcile_event_sources`, `event_source_thread` and
//! `fire_event` on four owned slots. Shared with desktop, unchanged: which
//! sources are wanted (`desired_event_sources`), which triggers a message
//! fires (`event_targets`), the live-only seen-set (`EventSource`), the
//! program args (`event_args`) and the run bookkeeping (`finish_run`, here
//! `finish_scheduled`, with the breaker). Managed-only, structurally:
//!
//! - at most four streams, the fixed `Task` slots; a slot is reused only
//!   after the owner joined its thread; a fifth wanted source is logged and
//!   counted as a capacity fault, never dropped silently;
//! - a source in another space is not watched — the host transport admits
//!   the working space only — and says so once in the log (desktop parity
//!   for the trigger record, which carries no marker: design §10 R8);
//! - records are handled sequentially on the source thread, as on desktop,
//!   each fire waiting — cancel- and close-aware — for the event lane's
//!   permit; the message is marked seen only once that permit is held
//!   (ADR-034 §2), and a wait a close ends drops it (live-only);
//! - a fired trigger is reserved until its report's bookkeeping returned, as
//!   a scheduled one is, so its next fire waits for its last write;
//! - a source thread exits at its next frame on any close, and convergence
//!   runs only while open: a reopen subscribes afresh (its snapshot seeds).
use super::*;
use crate::anyapi::AnyError;
use crate::chatwatch::records_in;
use crate::triggers::{
    desired_event_sources, event_args, event_targets, EventSource, CHAT_MESSAGES,
};

/// The event-source slots (`Task::EventSource0..3`).
const SLOTS: usize = 4;

/// A running source: its slot and its stop flag.
pub(super) struct Source {
    slot: usize,
    stop: Arc<AtomicBool>,
}

fn slot_task(slot: usize) -> Task {
    [
        Task::EventSource0,
        Task::EventSource1,
        Task::EventSource2,
        Task::EventSource3,
    ][slot]
}

impl Inner {
    /// Desktop's `reconcile_event_sources`, on the dispatcher: sources no
    /// trigger wants any more are stopped (their thread exits at its next
    /// frame), new ones get a free slot — while open only. Finished source
    /// threads are joined first, outside `state`, so their slots are free.
    pub(super) fn converge_sources(&self) {
        let free = self.free_slots();
        let mut spawn = Vec::new();
        {
            let mut st = self.lock();
            let open = self.open.load(Ordering::SeqCst);
            let desired = if open {
                desired_event_sources(&st.registry, &self.config.self_peer, &self.config.space)
            } else {
                BTreeMap::new()
            };
            for (key, source) in &st.sources {
                if !desired.contains_key(key) && !source.stop.swap(true, Ordering::SeqCst) {
                    info!("event source stopped: chat {} in space {}", key.1, key.0);
                }
            }
            if open {
                let g = self.gen.load(Ordering::SeqCst);
                let taken: BTreeSet<usize> = st.sources.values().map(|s| s.slot).collect();
                let mut free = free.into_iter().filter(|slot| !taken.contains(slot));
                for (key, triggers) in desired {
                    if st.sources.contains_key(&key) {
                        continue;
                    }
                    if key.0 != self.config.space {
                        if st.sources_noted.insert(key.clone()) {
                            info!(
                                "event source chat {} in space {} not watched: the host transport \
                                 admits the working space only ({triggers:?})",
                                key.1, key.0
                            );
                        }
                        continue;
                    }
                    let Some(slot) = free.next() else {
                        if st.sources_noted.insert(key.clone()) {
                            self.shared.capacity_fault();
                            warn!(
                                "event source chat {} in space {} not watched: all {SLOTS} source \
                                 slots are busy (a capacity fault; {triggers:?})",
                                key.1, key.0
                            );
                        }
                        continue;
                    };
                    st.sources_noted.remove(&key);
                    let stop = Arc::new(AtomicBool::new(false));
                    st.sources.insert(
                        key.clone(),
                        Source {
                            slot,
                            stop: stop.clone(),
                        },
                    );
                    info!(
                        "event source started: chat {} in space {} → {triggers:?}",
                        key.1, key.0
                    );
                    spawn.push((slot, key, stop, g));
                }
            }
        }
        for (slot, key, stop, g) in spawn {
            let Some(inner) = self.me.upgrade() else {
                return;
            };
            let (task_key, task_stop) = (key.clone(), stop.clone());
            let spawned = self.shared.spawn_owned(slot_task(slot), move |ctx| {
                inner.source_loop(&ctx, task_key, task_stop, g);
                Ok(())
            });
            if spawned.is_err() {
                // the owner is cancelled (or failed): the slot stays unused
                let mut st = self.lock();
                if st
                    .sources
                    .get(&key)
                    .is_some_and(|s| Arc::ptr_eq(&s.stop, &stop))
                {
                    st.sources.remove(&key);
                }
            }
        }
    }

    /// Slots whose thread the owner no longer holds; finished source threads
    /// are joined here (as the session's `join_finished` does), so a retired
    /// source's slot is reusable once its thread has exited.
    fn free_slots(&self) -> Vec<usize> {
        let mut owned = self.shared.owned.lock().unwrap_or_else(|e| e.into_inner());
        let mut index = 0;
        while index < owned.len() {
            let (task, handle) = &owned[index];
            if (0..SLOTS).any(|slot| slot_task(slot) == *task) && handle.is_finished() {
                let (task, handle) = owned.swap_remove(index);
                if handle.join().is_err() {
                    self.shared.fail(crate::managed::panic_failure(task));
                }
            } else {
                index += 1;
            }
        }
        (0..SLOTS)
            .filter(|slot| !owned.iter().any(|(task, _)| *task == slot_task(*slot)))
            .collect()
    }

    /// Still the source the generation `g` started, not stopped.
    fn source_live(&self, stop: &AtomicBool, g: u64) -> bool {
        !self.cancelled()
            && !stop.load(Ordering::SeqCst)
            && self.open.load(Ordering::SeqCst)
            && self.gen.load(Ordering::SeqCst) == g
    }

    /// Desktop's `event_source_thread`: the reconnect loop around one chat
    /// object's feed; a snapshot only seeds, `changes` fire. On exit the
    /// source takes its own entry out, so convergence may restart it.
    fn source_loop(&self, ctx: &TaskContext, key: (String, String), stop: Arc<AtomicBool>, g: u64) {
        let mut seen = EventSource::default();
        while self.source_live(&stop, g) {
            if let Err(e) = self.watch_source(&key, &stop, g, &mut seen) {
                warn!(
                    "event source {} in space {}: subscribe failed ({e}); retrying",
                    key.1, key.0
                );
            }
            ctx.idle_until(Instant::now() + self.config.cadence.reconnect);
        }
        let mut st = self.lock();
        if st
            .sources
            .get(&key)
            .is_some_and(|s| Arc::ptr_eq(&s.stop, &stop))
        {
            st.sources.remove(&key);
        }
    }

    fn watch_source(
        &self,
        key: &(String, String),
        stop: &AtomicBool,
        g: u64,
        seen: &mut EventSource,
    ) -> Result<(), AnyError> {
        let frames = self.client.subscribe_dataset(
            &key.0,
            &key.1,
            CHAT_MESSAGES,
            &json!({"sort": ["-createdAt"], "limit": 64}),
        )?;
        for frame in frames {
            if !self.source_live(stop, g) {
                return Ok(());
            }
            match frame.event.as_str() {
                "ready" => {}
                "closed" => return Ok(()),
                "snapshot" => {
                    let records = frame.data["records"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default();
                    seen.seed(&records);
                }
                "changes" => {
                    for record in records_in(&frame.data) {
                        if !self.fire_event(key, &record, stop, g, seen) {
                            return Ok(());
                        }
                    }
                }
                other => {
                    warn!("event source {}: unexpected frame {other:?}", key.1);
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    /// Desktop's `fire_event`: every trigger of this device naming the
    /// source, in order, each under the event lane's permit. False when a
    /// close or stop ended the wait (the record is dropped, live-only).
    fn fire_event(
        &self,
        key: &(String, String),
        record: &Value,
        stop: &AtomicBool,
        g: u64,
        seen: &mut EventSource,
    ) -> bool {
        if Watcher::is_self_message(record, &self.config.agent_name) {
            return true; // never self-triggered; the seen-set does the same
        }
        let targets = {
            let st = self.lock();
            event_targets(
                &st.registry,
                &self.config.self_peer,
                &self.config.space,
                &key.0,
                &key.1,
            )
        };
        let mut marked = false;
        for t in targets {
            let Some((mut st, permit)) = self.event_permit(&t.id, stop, g) else {
                return false;
            };
            // the seen mark under the permit, once per message (ADR-034 §2)
            if !marked {
                if seen
                    .fresh(vec![record.clone()], &self.config.agent_name)
                    .is_empty()
                {
                    return true; // a message this source already handled
                }
                marked = true;
            }
            // revalidated on the real entry: disabled, repinned or evicted
            // since the targets were read fires nothing (its permit dropped)
            let Some(entry) = st
                .registry
                .get(&t.id)
                .filter(|e| e.enabled && e.owner == self.config.self_peer)
                .cloned()
            else {
                continue;
            };
            info!(
                "event trigger {:?} fired by message {:?} on chat {}",
                entry.id,
                record.get("id").and_then(|v| v.as_str()).unwrap_or(""),
                key.1
            );
            self.bind_event(&mut st, entry, key, record, permit);
        }
        true
    }

    /// The event lane's permit for `trigger`, waited for like desktop's
    /// inline fire: also until a previous fire of the same trigger has
    /// finished its bookkeeping, and with a fresh grant (ADR-034 §7). None
    /// when a close, stop or cancel ended the wait.
    fn event_permit(
        &self,
        trigger: &str,
        stop: &AtomicBool,
        g: u64,
    ) -> Option<(MutexGuard<'_, State>, StorePermit)> {
        loop {
            if !self.source_live(stop, g) {
                return None;
            }
            let reports = self.reports.load(Ordering::SeqCst);
            if let Admission::Admitted(permit) = self.shared.pool.acquire(Lane::Event) {
                let st = self.lock();
                if !self.source_live(stop, g) {
                    return None;
                }
                if !st.reserved.contains_key(trigger) && st.grant_fresh() {
                    return Some((st, permit));
                }
                drop(st);
                drop(permit);
            }
            // a report frees the lane or the reservation: retry at once
            self.shared.pool.wait_until(self.config.cadence.wake, || {
                !self.source_live(stop, g) || self.reports.load(Ordering::SeqCst) != reports
            });
        }
    }

    /// Reserve, record and offer one event job (desktop's `run_trigger_program`
    /// on the lane); a refused offer — the pool closed meanwhile — releases
    /// the reservation at once.
    fn bind_event(
        &self,
        st: &mut State,
        entry: Trigger,
        key: &(String, String),
        record: &Value,
        permit: StorePermit,
    ) {
        let Some(lanes) = self.lanes.get() else {
            return;
        };
        let run_id = new_run_id();
        let job = LaneJob {
            lane: Lane::Event,
            work_id: entry.id.clone(),
            run_id: run_id.clone(),
            spec: entry.program.clone(),
            args: event_args(&entry, &key.0, &key.1, record),
            trigger: Some(entry.id.clone()),
            mailbox: Default::default(),
            interrupt: Arc::new(AtomicBool::new(false)),
        };
        st.reserved.insert(entry.id.clone(), run_id.clone());
        st.jobs.insert(
            run_id.clone(),
            JobKind::Event {
                trigger: entry.id.clone(),
            },
        );
        self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        if let Err(refused) = lanes.offer(job, permit, &self.shared) {
            drop(refused);
            info!(
                "event trigger {:?}: offer refused (stopping), not run",
                entry.id
            );
            self.cancelled_scheduled(st, &run_id, &entry.id, false);
            st.jobs.remove(&run_id);
            self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        }
    }
}
