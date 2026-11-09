//! Coupled lane admission and guest-memory ceilings (ADR-034 §2).
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use wasmtime::ResourceLimiter;

const MIB: usize = 1024 * 1024;
pub const MAX_PENDING: usize = 64;
const TOTAL_BYTES: usize = 768 * MIB;
/// Explicit per-store bounds (ADR-034 §2): the measured Stage-0 phone peak was
/// 11,520 table elements per CPython store; the kernel instantiates one
/// instance, one memory and a few funcref tables. Ceilings, not reservations.
pub const MAX_TABLE_ELEMENTS: usize = 65_536;
pub const MAX_INSTANCES: usize = 16;
pub const MAX_MEMORIES: usize = 4;
pub const MAX_TABLES: usize = 8;
/// Host-buffer bounds (ADR-034 §2): what the host itself holds per run and
/// per session, beside the guest ceilings above.
pub const MAX_MAILBOX_ITEMS: usize = 256;
pub const MAX_MAILBOX_BYTES: usize = MIB;
pub const MAX_BACKLOG: usize = 64;
pub const MAX_STREAM_LINE_BYTES: usize = 8 * MIB;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lane {
    Chat,
    Scheduled,
    Event,
}
impl Lane {
    fn bytes(self) -> usize {
        [256 * MIB, 384 * MIB, 128 * MIB][self as usize]
    }
    pub const ALL: [Lane; 3] = [Lane::Chat, Lane::Scheduled, Lane::Event];
}

/// Why the reopenable gate is closed (ADR-034 §2/§7). Admission is open only
/// while no cause holds: main's renewal (`Policy`) can never reopen a gate a
/// worker-local resource stand-down (`Resource`) closed, and §2's hysteresis
/// stays the only way back from memory pressure (Step B review F8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GateCause {
    /// Main's admission policy: closed until the first AppliedOpen, and again
    /// on every Close, expiry, Release and Stop.
    Policy = 1,
    /// Reserved for a worker-local stand-down cause. No worker code holds it:
    /// memory is enforced by Kotlin main through its own `Close`, the worker
    /// only reports its RSS (ADR-034 §2, user ruling 2026-11-06).
    Resource = 2,
}

#[derive(Default)]
struct Usage {
    occupied: [bool; 3],
    bytes: [usize; 3],
    total: usize,
}

struct Inner {
    usage: Mutex<Usage>,
    /// Occupied lanes, readable without the mutex (`health()`), and the
    /// condition the epoch driver waits on (ADR-034 §2: idle epochs stop).
    live: AtomicUsize,
    /// Permits attached to a real Store (`StorePermit::attach`): what the
    /// epoch driver ticks for — never a permit still waiting in an inbox.
    stores: AtomicUsize,
    changed: Condvar,
    /// Terminal: the generation is stopping (`Shared::on_stop`).
    closed: AtomicBool,
    /// Reopenable: the set of `GateCause` bits holding the gate closed. A
    /// stand-down (sync loss, resource pressure, a non-revoking or revoking
    /// Close) refuses new permits without ending the generation. Starts with
    /// `Policy` set: nothing is admitted before the first AppliedOpen (SB-I-10).
    gate_closed: AtomicU8,
    /// Bumped (under the usage lock) by every revoking stand-down and Stop.
    /// A permit records it at `acquire`, so a job admitted before a revocation
    /// and offered after it is still interrupted (Step B review F1).
    revocations: AtomicU64,
    /// Host-buffer saturations (mailbox, backlog, pending queue, leftovers,
    /// stream line), counted where they are enforced (R2-C8).
    capacity_faults: AtomicU64,
}

impl Default for Inner {
    fn default() -> Self {
        Inner {
            usage: Mutex::default(),
            live: AtomicUsize::new(0),
            stores: AtomicUsize::new(0),
            changed: Condvar::new(),
            closed: AtomicBool::new(false),
            gate_closed: AtomicU8::new(GateCause::Policy as u8),
            revocations: AtomicU64::new(0),
            capacity_faults: AtomicU64::new(0),
        }
    }
}

/// `StorePermit::attach` after the pool closed: the store must not start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Closed;
impl std::fmt::Display for Closed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("the lane pool is closed: no store may start")
    }
}
impl std::error::Error for Closed {}

/// One pool per managed generation, shared by every store and every lane.
#[derive(Clone, Default)]
pub struct LanePool(Arc<Inner>);

pub enum Admission {
    Admitted(StorePermit),
    CapacityDeferred,
}

impl LanePool {
    pub fn acquire(&self, lane: Lane) -> Admission {
        let mut usage = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        // closed, the gate and the revocation epoch are decided under the same
        // lock `close`, `close_gate` and `revoke` take: no permit after a close,
        // and a permit taken before a revocation carries the older epoch
        if self.0.closed.load(Ordering::Acquire)
            || self.0.gate_closed.load(Ordering::Acquire) != 0
            || usage.occupied[lane as usize]
        {
            return Admission::CapacityDeferred;
        }
        usage.occupied[lane as usize] = true;
        self.0.live.fetch_add(1, Ordering::AcqRel);
        self.0.changed.notify_all();
        Admission::Admitted(StorePermit {
            pool: self.clone(),
            lane,
            epoch: self.0.revocations.load(Ordering::Acquire),
            bytes: 0,
            pending: 0,
            tables: 0,
            pending_table: 0,
            attached: false,
        })
    }
    /// Occupied lanes (permits held, attached or not).
    pub fn occupied_lanes(&self) -> usize {
        self.0.live.load(Ordering::Acquire)
    }
    /// Permits attached to a live Store: what the epoch driver ticks for.
    pub fn live_stores(&self) -> usize {
        self.0.stores.load(Ordering::Acquire)
    }
    pub fn is_closed(&self) -> bool {
        self.0.closed.load(Ordering::Acquire)
    }
    /// Terminal stop (`Shared::on_stop` only): no further permit, no further
    /// attach, and every waiter wakes. A stand-down uses `close_gate`.
    pub fn close(&self) {
        let _guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        self.0.closed.store(true, Ordering::Release);
        self.0.changed.notify_all();
    }
    /// The reopenable admission gate (ADR-034 §5/§7): `cause` now holds it
    /// closed. Live permits are untouched.
    pub fn close_gate(&self, cause: GateCause) {
        let _guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        self.0.gate_closed.fetch_or(cause as u8, Ordering::AcqRel);
        self.0.changed.notify_all();
    }
    /// `cause` no longer holds the gate; it opens only when no other cause does.
    pub fn open_gate(&self, cause: GateCause) {
        let _guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        self.0
            .gate_closed
            .fetch_and(!(cause as u8), Ordering::AcqRel);
        self.0.changed.notify_all();
    }
    pub fn admission_open(&self) -> bool {
        self.0.gate_closed.load(Ordering::Acquire) == 0 && !self.is_closed()
    }
    /// A revocation (revoking stand-down, Stop): every permit taken before it
    /// is stale. Under the usage lock, so `acquire` reads either the old epoch
    /// with the gate still open or a closed gate.
    pub fn revoke(&self) {
        let _guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        self.0.revocations.fetch_add(1, Ordering::AcqRel);
    }
    /// The current revocation epoch (`StorePermit::epoch` compares against it).
    pub fn revocation_epoch(&self) -> u64 {
        self.0.revocations.load(Ordering::Acquire)
    }
    /// Count one host-buffer saturation (a visible capacity fault, ADR-034 §2).
    pub fn capacity_fault(&self) {
        self.0.capacity_faults.fetch_add(1, Ordering::Relaxed);
    }
    pub fn capacity_faults(&self) -> u64 {
        self.0.capacity_faults.load(Ordering::Relaxed)
    }
    /// Wake every waiter (a lane inbox changed, a run finished).
    pub fn notify(&self) {
        let _guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        self.0.changed.notify_all();
    }
    /// Block until `done()`, at most `timeout`; every permit change, `notify`,
    /// a gate change and `close` re-checks the predicate. A waiter that must
    /// end on stop puts the cancel in its predicate — the pool never returns
    /// early on `closed` by itself, so no loop spins on a closed pool (R2-C5).
    pub fn wait_until(&self, timeout: Duration, mut done: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + timeout;
        let mut guard = self.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            if done() {
                return true;
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return false;
            };
            guard = self
                .0
                .changed
                .wait_timeout(guard, remaining)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }
    /// Block until a store is live — or, once the pool is closed, until no
    /// permit is occupied either (a taken permit may still attach: R2-C1) —
    /// at most `timeout`. Returns the live count observed on wake.
    pub fn wait_live(&self, timeout: Duration) -> usize {
        self.wait_until(timeout, || {
            self.0.stores.load(Ordering::Acquire) > 0
                || (self.is_closed() && self.0.live.load(Ordering::Acquire) == 0)
        });
        self.0.stores.load(Ordering::Acquire)
    }
}

#[cfg(test)]
impl LanePool {
    /// A pool whose policy gate is open (an AppliedOpen in force).
    pub fn opened() -> Self {
        let pool = Self::default();
        pool.open_gate(GateCause::Policy);
        pool
    }
}

/// Owns a lane until the Store drops, including failed instantiation/unwind.
pub struct StorePermit {
    pool: LanePool,
    lane: Lane,
    /// The revocation epoch at `acquire`.
    epoch: u64,
    bytes: usize,
    pending: usize,
    /// table elements of this store, summed across its tables (ADR-034 §2)
    tables: usize,
    pending_table: usize,
    attached: bool,
}
impl StorePermit {
    pub fn lane(&self) -> Lane {
        self.lane
    }
    /// The pool's revocation epoch when this permit was taken: a later
    /// revocation makes the permit's job stale (interrupted, never run).
    pub fn epoch(&self) -> u64 {
        self.epoch
    }
    /// A revocation ran since this permit was taken.
    pub fn revoked(&self) -> bool {
        self.pool.revocation_epoch() > self.epoch
    }
    /// The permit now limits a real Store (`runner::prepare_store_with`): one
    /// idempotent step under the usage lock, refused once the pool closed so
    /// no store starts after Stop (R2-C1, I-7).
    pub(crate) fn attach(&mut self) -> Result<(), Closed> {
        let _guard = self.pool.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        if self.pool.0.closed.load(Ordering::Acquire) {
            return Err(Closed);
        }
        if !self.attached {
            self.attached = true;
            self.pool.0.stores.fetch_add(1, Ordering::AcqRel);
            self.pool.0.changed.notify_all();
        }
        Ok(())
    }
    /// Commit an already selected due occurrence only after lane acquisition.
    /// Run completion/failure accounting remains Scheduler::record_run's job.
    pub fn consume_scheduled(
        &self,
        scheduler: &crate::triggers::Scheduler,
        trigger: &mut crate::triggers::Trigger,
    ) -> bool {
        if self.lane != Lane::Scheduled {
            return false;
        }
        let mut candidate = trigger.clone();
        if candidate.kind == "cron" && scheduler.cron_due(&mut candidate) {
            scheduler.advance_cron(trigger);
            true
        } else if scheduler.once_due(trigger) {
            trigger.enabled = false;
            true
        } else {
            false
        }
    }
}
impl ResourceLimiter for StorePermit {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.pending = 0;
        let Some(delta) = desired.checked_sub(current) else {
            return Ok(false);
        };
        let mut usage = self.pool.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        if maximum.is_some_and(|max| desired > max)
            || delta
                > self
                    .lane
                    .bytes()
                    .saturating_sub(usage.bytes[self.lane as usize])
            || delta > TOTAL_BYTES.saturating_sub(usage.total)
        {
            return Ok(false);
        }
        usage.bytes[self.lane as usize] += delta;
        usage.total += delta;
        self.bytes += delta;
        self.pending = delta;
        Ok(true)
    }
    fn memory_grow_failed(&mut self, _: wasmtime::Error) -> wasmtime::Result<()> {
        let mut usage = self.pool.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        usage.bytes[self.lane as usize] -= self.pending;
        usage.total -= self.pending;
        self.bytes -= self.pending;
        self.pending = 0;
        Ok(())
    }
    /// Explicit table bound (ADR-034 §2): the store's tables together may not
    /// exceed `MAX_TABLE_ELEMENTS`; growth, including the instantiation
    /// request, is refused past it or past the table's own maximum.
    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.pending_table = 0;
        let Some(delta) = desired.checked_sub(current) else {
            return Ok(false);
        };
        if maximum.is_some_and(|max| desired > max)
            || delta > MAX_TABLE_ELEMENTS.saturating_sub(self.tables)
        {
            return Ok(false);
        }
        self.tables += delta;
        self.pending_table = delta;
        Ok(true)
    }
    fn table_grow_failed(&mut self, _: wasmtime::Error) -> wasmtime::Result<()> {
        self.tables -= self.pending_table;
        self.pending_table = 0;
        Ok(())
    }
    fn instances(&self) -> usize {
        MAX_INSTANCES
    }
    fn memories(&self) -> usize {
        MAX_MEMORIES
    }
    fn tables(&self) -> usize {
        MAX_TABLES
    }
}
impl Drop for StorePermit {
    fn drop(&mut self) {
        let mut usage = self.pool.0.usage.lock().unwrap_or_else(|e| e.into_inner());
        usage.bytes[self.lane as usize] -= self.bytes;
        usage.total -= self.bytes;
        usage.occupied[self.lane as usize] = false;
        self.pool.0.live.fetch_sub(1, Ordering::AcqRel);
        if self.attached {
            self.pool.0.stores.fetch_sub(1, Ordering::AcqRel);
        }
        self.pool.0.changed.notify_all();
    }
}

/// Fixed descriptors only; guest inputs stay in their source until admission.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingWork {
    pub id: String,
    pub lane: Lane,
    pub heartbeat: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum QueueError {
    CapacityFault,
    InvalidDescriptor,
}

#[derive(Default)]
pub struct PendingQueue(VecDeque<PendingWork>);
impl PendingQueue {
    /// A saturated queue is counted on `pool` as a capacity fault where it is
    /// refused (R2-C8), never left to the caller to remember.
    pub fn push(&mut self, work: PendingWork, pool: &LanePool) -> Result<(), QueueError> {
        if work.id.is_empty()
            || work.id.len() > 256
            || !work.id.is_ascii()
            || (work.heartbeat && work.lane != Lane::Scheduled)
        {
            return Err(QueueError::InvalidDescriptor);
        }
        if self
            .0
            .iter()
            .any(|item| item.id == work.id && item.lane == work.lane)
        {
            return Ok(());
        }
        if self.0.len() == MAX_PENDING {
            pool.capacity_fault();
            return Err(QueueError::CapacityFault);
        }
        self.0.push_back(work);
        Ok(())
    }
    pub fn len(&self) -> usize {
        self.0.len()
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
    /// The caller consumes once/cron/event state only after receiving the permit.
    pub fn admit(&mut self, pool: &LanePool) -> Option<(PendingWork, StorePermit)> {
        let mut order: Vec<_> = (0..self.0.len()).collect();
        order.sort_by_key(|i| !self.0[*i].heartbeat);
        for index in order {
            if let Admission::Admitted(permit) = pool.acquire(self.0[index].lane) {
                return Some((self.0.remove(index).unwrap(), permit));
            }
        }
        None
    }
    pub fn cancel(&mut self) {
        self.0.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn permit(pool: &LanePool, lane: Lane) -> StorePermit {
        match pool.acquire(lane) {
            Admission::Admitted(p) => p,
            _ => panic!("busy"),
        }
    }
    #[test]
    fn all_memories_share_lane_and_generation_ceiling_and_failed_growth_rolls_back() {
        let pool = LanePool::opened();
        let mut chat = permit(&pool, Lane::Chat);
        let mut scheduled = permit(&pool, Lane::Scheduled);
        let mut event = permit(&pool, Lane::Event);
        assert_eq!(pool.occupied_lanes(), 3);
        assert_eq!(pool.live_stores(), 0, "no store attached yet");
        chat.attach().unwrap();
        chat.attach().unwrap(); // idempotent
        assert_eq!(pool.live_stores(), 1);
        assert!(matches!(
            pool.acquire(Lane::Chat),
            Admission::CapacityDeferred
        ));
        assert!(chat.memory_growing(0, 128 * MIB, None).unwrap());
        assert!(chat.memory_growing(0, 128 * MIB, None).unwrap());
        assert!(!chat.memory_growing(0, 1, None).unwrap());
        assert!(scheduled.memory_growing(0, 384 * MIB, None).unwrap());
        assert!(event.memory_growing(0, 128 * MIB, None).unwrap());
        assert_eq!(pool.0.usage.lock().unwrap().total, TOTAL_BYTES);
        event
            .memory_grow_failed(wasmtime::Error::msg("allocation failed"))
            .unwrap();
        assert_eq!(pool.0.usage.lock().unwrap().total, 640 * MIB);
        drop((chat, scheduled, event));
        assert_eq!(pool.live_stores(), 0);
        assert_eq!(pool.occupied_lanes(), 0);
        assert_eq!(pool.0.usage.lock().unwrap().total, 0);
    }
    #[test]
    fn tables_and_counts_have_explicit_bounds_and_a_closed_pool_admits_nothing() {
        let pool = LanePool::opened();
        let mut chat = permit(&pool, Lane::Chat);
        // the bound is the store's tables summed: the measured 11,520 plus a second
        // table up to the ceiling, then nothing more; a failed growth rolls back
        assert!(chat.table_growing(0, 11_520, None).unwrap());
        assert!(chat
            .table_growing(0, MAX_TABLE_ELEMENTS - 11_520, None)
            .unwrap());
        // the accepted second growth failed to allocate: its reservation rolls back
        chat.table_grow_failed(wasmtime::Error::msg("allocation failed"))
            .unwrap();
        assert!(chat.table_growing(0, 1, None).unwrap());
        assert!(!chat.table_growing(0, MAX_TABLE_ELEMENTS, None).unwrap());
        assert!(!chat.table_growing(0, 10, Some(9)).unwrap());
        assert_eq!(
            (chat.instances(), chat.memories(), chat.tables()),
            (MAX_INSTANCES, MAX_MEMORIES, MAX_TABLES)
        );
        assert_eq!(pool.wait_live(Duration::from_millis(1)), 0, "not attached");
        chat.attach().unwrap();
        assert_eq!(pool.wait_live(Duration::from_millis(1)), 1);
        drop(chat);
        assert_eq!(pool.wait_live(Duration::from_millis(1)), 0);
        // the reopenable gate: a stand-down refuses, a reopen admits again
        pool.close_gate(GateCause::Policy);
        assert!(!pool.admission_open());
        assert!(matches!(
            pool.acquire(Lane::Chat),
            Admission::CapacityDeferred
        ));
        pool.open_gate(GateCause::Policy);
        let mut late = permit(&pool, Lane::Chat);
        // the terminal close: nothing admitted, and a taken permit cannot attach
        pool.close();
        assert!(!pool.admission_open());
        assert_eq!(late.attach(), Err(Closed));
        assert_eq!(pool.live_stores(), 0);
        assert!(matches!(
            pool.acquire(Lane::Scheduled),
            Admission::CapacityDeferred
        ));
        assert!(PendingQueue::default().admit(&pool).is_none());
    }
    /// F8 / SB-I-10: the gate starts closed (nothing is admitted before the
    /// first AppliedOpen) and opens only when no cause holds, so main's
    /// renewal never overrides a resource stand-down. F1: a permit carries the
    /// revocation epoch of its `acquire`.
    #[test]
    fn the_gate_starts_closed_opens_only_when_no_cause_holds_and_permits_carry_their_epoch() {
        let pool = LanePool::default();
        assert!(
            !pool.admission_open(),
            "closed before the first AppliedOpen"
        );
        assert!(matches!(
            pool.acquire(Lane::Chat),
            Admission::CapacityDeferred
        ));
        pool.open_gate(GateCause::Policy);
        assert!(pool.admission_open());
        pool.close_gate(GateCause::Resource);
        pool.close_gate(GateCause::Policy);
        pool.open_gate(GateCause::Policy); // main's 5 s renewal
        assert!(
            !pool.admission_open(),
            "a renewal cannot override memory pressure"
        );
        assert!(matches!(
            pool.acquire(Lane::Chat),
            Admission::CapacityDeferred
        ));
        pool.open_gate(GateCause::Resource);
        let taken = permit(&pool, Lane::Chat);
        assert_eq!(taken.epoch(), 0);
        assert!(!taken.revoked());
        pool.revoke();
        assert!(taken.revoked(), "taken before the revocation: stale");
        drop(taken);
        let fresh = permit(&pool, Lane::Chat);
        assert_eq!(fresh.epoch(), 1);
        assert!(!fresh.revoked());
    }
    #[test]
    fn wasmtime_store_uses_the_permit_across_multiple_memories_and_releases_it() {
        let pool = LanePool::opened();
        let engine = wasmtime::Engine::default();
        let mut store = wasmtime::Store::new(&engine, permit(&pool, Lane::Chat));
        store.limiter(|permit| permit);
        let first = wasmtime::Memory::new(&mut store, wasmtime::MemoryType::new(1, None)).unwrap();
        let _second =
            wasmtime::Memory::new(&mut store, wasmtime::MemoryType::new(1, None)).unwrap();
        assert_eq!(pool.0.usage.lock().unwrap().total, 2 * 65536);
        assert!(first.grow(&mut store, 4095).is_err());
        assert_eq!(pool.0.usage.lock().unwrap().total, 2 * 65536);
        let table = wasmtime::Table::new(
            &mut store,
            wasmtime::TableType::new(wasmtime::RefType::FUNCREF, 1, None),
            wasmtime::Ref::Func(None),
        )
        .unwrap();
        assert!(table
            .grow(
                &mut store,
                MAX_TABLE_ELEMENTS as u64,
                wasmtime::Ref::Func(None)
            )
            .is_err());
        drop(store);
        assert_eq!(pool.live_stores(), 0);
        assert_eq!(pool.0.usage.lock().unwrap().total, 0);
    }
    #[test]
    fn deferred_once_and_cron_do_not_mutate_and_heartbeat_precedes_due_job() {
        use crate::triggers::{Scheduler, Trigger};
        let pool = LanePool::opened();
        let busy = permit(&pool, Lane::Scheduled);
        let mut queue = PendingQueue::default();
        let mut trigger = Trigger::cron(
            "once",
            "once",
            30.,
            "fixture@v1",
            serde_json::json!({}),
            "peer",
            true,
        );
        trigger.kind = "once".into();
        trigger.spec = serde_json::json!({"at": 1.});
        let mut scheduler = Scheduler::new("peer", Box::new(|| 100.));
        scheduler.arm();
        queue
            .push(
                PendingWork {
                    id: trigger.id.clone(),
                    lane: Lane::Scheduled,
                    heartbeat: false,
                },
                &pool,
            )
            .unwrap();
        queue
            .push(
                PendingWork {
                    id: "heartbeat".into(),
                    lane: Lane::Scheduled,
                    heartbeat: true,
                },
                &pool,
            )
            .unwrap();
        assert!(scheduler.once_due(&trigger));
        assert!(queue.admit(&pool).is_none());
        assert!(trigger.last_run_at.is_none());
        assert!(trigger.next_due.is_none());
        assert_eq!(trigger.consecutive_failures, 0);
        drop(busy);
        let (work, active) = queue.admit(&pool).unwrap();
        assert_eq!(work.id, "heartbeat");
        assert!(queue.admit(&pool).is_none());
        drop(active);
        let (work, permit) = queue.admit(&pool).unwrap();
        assert_eq!(work.id, "once");
        assert!(permit.consume_scheduled(&scheduler, &mut trigger));
        assert!(!scheduler.once_due(&trigger));
        assert!(trigger.last_run_at.is_none());
        assert_eq!(trigger.consecutive_failures, 0);
        let mut cron = Trigger::cron(
            "cron",
            "cron",
            30.,
            "fixture@v1",
            serde_json::json!({}),
            "peer",
            true,
        );
        cron.next_due = Some(99.);
        assert!(permit.consume_scheduled(&scheduler, &mut cron));
        assert_eq!(cron.next_due, Some(130.));
    }
    #[test]
    fn saturation_is_visible_deduplicated_and_standdown_clears_all_lanes() {
        let pool = LanePool::opened();
        let mut queue = PendingQueue::default();
        for i in 0..MAX_PENDING {
            queue
                .push(
                    PendingWork {
                        id: i.to_string(),
                        lane: Lane::Event,
                        heartbeat: false,
                    },
                    &pool,
                )
                .unwrap();
        }
        let extra = PendingWork {
            id: "extra".into(),
            lane: Lane::Chat,
            heartbeat: false,
        };
        assert_eq!(queue.push(extra, &pool), Err(QueueError::CapacityFault));
        assert_eq!(
            pool.capacity_faults(),
            1,
            "counted where it is refused (R2-C8)"
        );
        queue
            .push(
                PendingWork {
                    id: "0".into(),
                    lane: Lane::Event,
                    heartbeat: false,
                },
                &pool,
            )
            .unwrap();
        assert_eq!(queue.len(), MAX_PENDING);
        assert_eq!(
            pool.capacity_faults(),
            1,
            "a deduplicated push is not a fault"
        );
        queue.cancel();
        assert!(queue.is_empty());
        assert!(queue.admit(&LanePool::opened()).is_none());
    }
}
