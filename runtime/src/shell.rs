//! shell — the `sh.*` / `fs.*` syscalls (ADR-024), compiled only with
//! `--features shell`. Without the feature the broker has no arm for
//! these names and answers `unknown effect`; the capability is not in
//! the binary (ADR-024 §6).
//!
//! Every call here is a recorded effect like any other: `sh.*` and the
//! `fs` writes classify `mutate` (never re-executed on replay), `fs`
//! reads classify `read`. Nothing is confined (ADR-024 §5) — a command
//! does what the serve's user can do.

use crate::broker::EffectFailure;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{BufRead, Read, Write};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

#[cfg(not(unix))]
compile_error!("the `shell` feature needs a unix host (process groups, SIGKILL)");

/// Per-stream capture cap (ADR-024 §1, resolved Q3: starts at 1 MiB,
/// unmeasured). Over it, head + tail are kept and `truncated` is set.
pub const STREAM_CAP: usize = 1 << 20;
/// `fs.read` text cap — same number, same reason. Bytes are not
/// capped: `encoding: "blob"` streams the file into the blob
/// directory and returns a ref (ADR-026 §4).
pub const FILE_CAP: usize = STREAM_CAP;
/// `fs.edit` rewrites the whole file: refuse anything a text edit has
/// no business touching (16 MiB) before allocating it.
pub const EDIT_CAP: usize = 16 << 20;
/// `fs.list` entry cap.
pub const LIST_CAP: usize = 5000;
const DEFAULT_TIMEOUT_S: f64 = 120.0;
const POLL_MS: u64 = 20;
/// The pipe pumps wait on poll(2) this long per tick so they can
/// observe their stop flag instead of parking in read(2) forever.
const PUMP_POLL_MS: i32 = 100;
/// Grace for the pipe pumps after the process exited: a grandchild
/// that kept the pipe open (a daemon the command left behind) must not
/// wedge the cell — we return what was captured so far and the pumps
/// stop on their next tick (they hold no fd after that).
const READER_GRACE: Duration = Duration::from_secs(2);
/// The one login shell per process (`login_env`) gets this long; a
/// profile that hangs or leaves a daemon on stdout falls through to
/// the inherit-env path instead of parking every later `sh.run`.
const LOGIN_SNAPSHOT_TIMEOUT: Duration = Duration::from_secs(10);

/// What the broker hands over per call: the run's interrupt flag (a
/// hard break kills the child too — ADR-003 §2 amendment) and the
/// cell's wall deadline (`timeout_s` is clamped to it).
pub struct Ctx {
    pub interrupt: Arc<AtomicBool>,
    pub deadline: Option<Instant>,
    /// The program that runs commands: bash, resolved on the login
    /// PATH (the tool is called `bash` and the model writes bash —
    /// heredocs, `$(...)`, arrays — whatever the user's own shell is);
    /// `/bin/sh` only when no bash exists. Always invoked `-c <cmd>`.
    pub shell: String,
    /// The user's login shell (`$SHELL`): the environment snapshot
    /// comes from it, commands do not run in it.
    pub login_shell: String,
    /// Run commands in the environment snapshotted once from a login
    /// shell (ADR-024 resolved Q1) instead of the serve's own. Tests
    /// turn it off for determinism.
    pub login_env: bool,
    /// The run's blob directory (ADR-026 §1): `fs.read(encoding="blob")`
    /// copies the file into it and hands back a ref, `fs.write` with a
    /// ref body streams the bytes out of it. `None` = no directory in
    /// this runtime (every byte leg fails typed).
    pub blob_dir: Option<crate::blob::BlobDir>,
}

impl Ctx {
    pub fn from_env(interrupt: Arc<AtomicBool>, deadline: Option<Instant>) -> Self {
        let login_shell = login_shell_from_env();
        Ctx {
            interrupt,
            deadline,
            shell: command_shell(login_env(&login_shell).and_then(|e| e.get("PATH").cloned())),
            login_shell,
            login_env: true,
            blob_dir: None,
        }
    }

    pub fn with_blob_dir(mut self, dir: Option<crate::blob::BlobDir>) -> Self {
        self.blob_dir = dir;
        self
    }

    fn blob_dir(&self) -> Result<&crate::blob::BlobDir, EffectFailure> {
        self.blob_dir.as_ref().ok_or_else(|| {
            fail(
                "blob_missing",
                "no blob directory in this runtime — bytes cannot leave the trace",
            )
        })
    }
}

/// `$SHELL`, `/bin/sh` when unset — the LOGIN shell (env snapshot).
pub fn login_shell_from_env() -> String {
    match std::env::var("SHELL") {
        Ok(s) if !s.is_empty() => s,
        _ => "/bin/sh".into(),
    }
}

/// The bash that runs commands: first `bash` on `path` (the login
/// PATH — nix/homebrew bash before the system one), then the usual
/// system locations, else `/bin/sh` (and the model's bashisms fail
/// loudly with the shell's own message).
pub fn command_shell(path: Option<String>) -> String {
    let env_path = path.unwrap_or_else(|| std::env::var("PATH").unwrap_or_default());
    let dirs = env_path
        .split(':')
        .filter(|d| !d.is_empty())
        .map(PathBuf::from)
        .chain(["/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"].map(PathBuf::from));
    for dir in dirs {
        let p = dir.join("bash");
        if p.is_file() {
            return p.to_string_lossy().into_owned();
        }
    }
    "/bin/sh".into()
}

// --- login environment snapshot ----------------------------------------------
//
// The serve may be launched by the desktop app with a bare PATH while
// the toolchain (nix, uv, cargo) lives in the login profile. Running
// every command through `$SHELL -lc` pays the profile per call AND
// leaks whatever the profile prints (the first smoke showed an
// xterm-title escape in stdout). So: one login shell per process,
// `env -0` behind a marker, cached; commands run `$SHELL -c` in it.

static LOGIN_ENV: OnceLock<Option<BTreeMap<String, String>>> = OnceLock::new();
const ENV_MARK: &str = "__ANYRT_ENV_SNAPSHOT__";

/// The login environment, snapshotted on first use. None when the
/// shell could not be run or printed nothing usable — callers then
/// fall back to inheriting the serve's environment.
pub fn login_env(shell: &str) -> Option<&'static BTreeMap<String, String>> {
    LOGIN_ENV
        .get_or_init(|| {
            let out = run_capture(
                shell,
                &["-lc", &format!("printf '{ENV_MARK}'; env -0")],
                LOGIN_SNAPSHOT_TIMEOUT,
            )?;
            parse_env_snapshot(&out)
        })
        .as_ref()
}

/// Run `program args` with no stdin and capture stdout, bounded by
/// `timeout` (the process group is killed on expiry → None). The
/// snapshot's runner; never used for guest commands (those need the
/// full `sh_run` contract).
fn run_capture(program: &str, args: &[&str], timeout: Duration) -> Option<Vec<u8>> {
    use std::os::unix::process::CommandExt;
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .ok()?;
    let pid = child.id();
    let out = pump(child.stdout.take()?);
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(POLL_MS))
            }
            _ => {
                kill_group(pid);
                let _ = child.wait();
                break None;
            }
        }
    };
    kill_group(pid);
    let bytes = out.finish_bytes(Instant::now() + READER_GRACE);
    status.filter(|s| s.success()).map(|_| bytes)
}

fn parse_env_snapshot(bytes: &[u8]) -> Option<BTreeMap<String, String>> {
    let mark = ENV_MARK.as_bytes();
    let pos = bytes.windows(mark.len()).position(|w| w == mark)?;
    let rest = &bytes[pos + mark.len()..];
    let mut env = BTreeMap::new();
    for chunk in rest.split(|b| *b == 0) {
        let Ok(s) = std::str::from_utf8(chunk) else {
            continue;
        };
        let Some((k, v)) = s.split_once('=') else {
            continue;
        };
        let ident = !k.is_empty()
            && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            && !k.starts_with(|c: char| c.is_ascii_digit());
        if ident {
            env.insert(k.to_string(), v.to_string());
        }
    }
    (!env.is_empty()).then_some(env)
}

/// `runtime.get("shell")` (ADR-024 §4): where bao is, so the first
/// cell doesn't probe with `pwd`.
pub fn runtime_value() -> Value {
    let login = login_shell_from_env();
    let shell = command_shell(login_env(&login).and_then(|e| e.get("PATH").cloned()));
    json!({
        "cwd": std::env::current_dir().ok().map(|p| p.to_string_lossy().into_owned()),
        "home": std::env::var("HOME").ok(),
        "shell": shell,
        "os": std::env::consts::OS,
    })
}

pub fn owns(name: &str) -> bool {
    name.starts_with("sh.") || name.starts_with("fs.")
}

/// ADR-024 §1/§2: every `sh.*` is mutate (a shell is never safely
/// re-executable); `fs` reads are reads, writes are mutate.
pub fn classify(name: &str) -> &'static str {
    match name {
        "fs.read" | "fs.list" => "read",
        _ => "mutate",
    }
}

/// Mask per-call `env` values whose NAME looks like a credential before
/// the input is recorded (ADR-024 §1). Deterministic, so the replay key
/// computed from the masked input matches on the next run.
pub fn redact_input(name: &str, payload: &Value) -> Value {
    if !name.starts_with("sh.") {
        return payload.clone();
    }
    let Some(env) = payload.get("env").and_then(|e| e.as_object()) else {
        return payload.clone();
    };
    let mut out = payload.as_object().cloned().unwrap_or_default();
    let masked: Map<String, Value> = env
        .iter()
        .map(|(k, v)| {
            let up = k.to_uppercase();
            let sensitive = ["TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL"]
                .iter()
                .any(|s| up.contains(s));
            (k.clone(), if sensitive { json!("***") } else { v.clone() })
        })
        .collect();
    out.insert("env".into(), Value::Object(masked));
    Value::Object(out)
}

pub fn execute(ctx: &Ctx, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
    match name {
        "sh.run" => sh_run(ctx, payload),
        "sh.spawn" | "sh.poll" | "sh.kill" => Err(fail(
            "NotImplementedError",
            format!("{name}: not in this build (ADR-024 resolved Q4) — use sh.run, or tmux for long-running work"),
        )),
        "fs.read" => fs_read(ctx, payload),
        "fs.list" => fs_list(payload),
        "fs.write" => fs_write(ctx, payload),
        "fs.edit" => fs_edit(payload),
        _ => Err(fail("KeyError", format!("unknown effect {name}"))),
    }
}

fn fail(type_: &str, message: impl Into<String>) -> EffectFailure {
    EffectFailure {
        type_: type_.into(),
        message: message.into(),
    }
}

fn str_arg<'a>(payload: &'a Value, key: &str) -> Result<&'a str, EffectFailure> {
    payload
        .get(key)
        .and_then(|v| v.as_str())
        .ok_or_else(|| fail("TypeError", format!("missing string argument {key:?}")))
}

// --- bounded capture ---------------------------------------------------------

/// Head + tail of a byte stream under a cap: the first half is kept
/// verbatim, the last half rides a ring; the middle is counted, not
/// stored.
struct Capture {
    head: Vec<u8>,
    tail: VecDeque<u8>,
    dropped: usize,
    cap: usize,
}

impl Capture {
    fn new(cap: usize) -> Self {
        Capture {
            head: Vec::new(),
            tail: VecDeque::new(),
            dropped: 0,
            cap,
        }
    }

    fn push(&mut self, buf: &[u8]) {
        let half = self.cap / 2;
        let mut rest = buf;
        if self.head.len() < half {
            let take = (half - self.head.len()).min(rest.len());
            self.head.extend_from_slice(&rest[..take]);
            rest = &rest[take..];
        }
        for &b in rest {
            if self.tail.len() == half {
                self.tail.pop_front();
                self.dropped += 1;
            }
            self.tail.push_back(b);
        }
    }

    #[cfg(test)]
    fn total(&self) -> usize {
        self.head.len() + self.tail.len() + self.dropped
    }

    /// Everything captured, in order — only meaningful under the cap.
    fn bytes(&self) -> Vec<u8> {
        let mut b = self.head.clone();
        b.extend(self.tail.iter());
        b
    }

    fn render(&self) -> (String, bool) {
        let mut bytes = self.head.clone();
        if self.dropped > 0 {
            bytes.extend_from_slice(format!("\n[… {} bytes elided …]\n", self.dropped).as_bytes());
        }
        bytes.extend(self.tail.iter());
        (
            String::from_utf8_lossy(&bytes).into_owned(),
            self.dropped > 0,
        )
    }
}

/// A pipe reader on its own thread. It waits in poll(2), not read(2),
/// so `stop` ends it even when the far end never closes (a setsid'd
/// grandchild the group kill cannot reach): the thread exits, the fd
/// is dropped, nothing leaks.
struct Pump {
    cap: Arc<Mutex<Capture>>,
    done: mpsc::Receiver<()>,
    stop: Arc<AtomicBool>,
}

impl Pump {
    /// Wait until `deadline` (shared by every pump of one command) for
    /// EOF; a pump still open after that is told to stop — the command
    /// left something holding its pipe — and what was captured is
    /// returned either way.
    fn drain(&self, deadline: Instant) {
        let left = deadline.saturating_duration_since(Instant::now());
        if self.done.recv_timeout(left).is_err() {
            self.stop.store(true, Ordering::Relaxed);
            tracing::warn!(
                "sh.run: a pipe stayed open past the post-exit grace (a daemon the command left behind?) — capture closed"
            );
        }
    }

    fn finish(self, deadline: Instant) -> (String, bool) {
        self.drain(deadline);
        let out = self.cap.lock().unwrap().render();
        out
    }

    fn finish_bytes(self, deadline: Instant) -> Vec<u8> {
        self.drain(deadline);
        let out = self.cap.lock().unwrap().bytes();
        out
    }
}

/// poll(2) one fd for `events`; Some(true) = ready (or hung up),
/// Some(false) = timed out, None = poll failed (not EINTR).
fn poll_fd(fd: i32, events: i16) -> Option<bool> {
    let mut pfd = libc::pollfd {
        fd,
        events,
        revents: 0,
    };
    loop {
        // SAFETY: one valid pollfd, count 1.
        let r = unsafe { libc::poll(&mut pfd, 1, PUMP_POLL_MS) };
        if r < 0 {
            if std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return None;
        }
        return Some(r > 0);
    }
}

fn pump(mut reader: impl Read + AsRawFd + Send + 'static) -> Pump {
    let cap = Arc::new(Mutex::new(Capture::new(STREAM_CAP)));
    let (tx, rx) = mpsc::channel();
    let stop = Arc::new(AtomicBool::new(false));
    let (sink, flag) = (cap.clone(), stop.clone());
    std::thread::spawn(move || {
        let fd = reader.as_raw_fd();
        let mut buf = [0u8; 8192];
        while !flag.load(Ordering::Relaxed) {
            match poll_fd(fd, libc::POLLIN) {
                Some(true) => match reader.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => sink.lock().unwrap().push(&buf[..n]),
                },
                Some(false) => continue,
                None => break,
            }
        }
        let _ = tx.send(());
    });
    Pump {
        cap,
        done: rx,
        stop,
    }
}

/// Feed the child's stdin on its own thread, poll(2)-paced like the
/// readers: a child that emits while it reads (cat, sort, jq) fills
/// its stdout pipe and stops reading — a blocking write_all here would
/// deadlock against our own unread pipe. The output pumps exist before
/// this starts; `stop` ends it if the pipe never drains.
fn feed_stdin(mut stdin: std::process::ChildStdin, text: String) -> Arc<AtomicBool> {
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    std::thread::spawn(move || {
        let fd = stdin.as_raw_fd();
        let bytes = text.as_bytes();
        let mut at = 0;
        while at < bytes.len() && !flag.load(Ordering::Relaxed) {
            match poll_fd(fd, libc::POLLOUT) {
                Some(true) => match stdin.write(&bytes[at..(at + 65536).min(bytes.len())]) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => at += n,
                },
                Some(false) => continue,
                None => break,
            }
        }
        drop(stdin); // EOF — a command reading stdin must never wait on us
    });
    stop
}

// --- sh.run ------------------------------------------------------------------

/// Process groups with a live `sh.run` in this process. A command's
/// group is killed when the command exits (call-scoped — a `cmd &`
/// left behind dies with the call, ADR-024 §1); this set exists for
/// the serve's own exit: `kill_all` on `AgentHandle::stop()` so a run
/// mid-command leaves nothing behind. On Linux the child additionally
/// carries PDEATHSIG for the case the serve dies without `stop()`.
static LIVE: Mutex<BTreeSet<u32>> = Mutex::new(BTreeSet::new());

/// Kill every live command's process group (serve shutdown).
pub fn kill_all() {
    let pids: Vec<u32> = LIVE.lock().unwrap().iter().copied().collect();
    for pid in pids {
        kill_group(pid);
    }
}

fn kill_group(pid: u32) {
    // SAFETY: plain libc call on a pid we spawned as a group leader
    // (`process_group(0)` → pgid == pid). ESRCH on an already-gone
    // group is fine.
    unsafe {
        libc::killpg(pid as libc::pid_t, libc::SIGKILL);
    }
}

fn sh_run(ctx: &Ctx, payload: &Value) -> Result<Value, EffectFailure> {
    use std::os::unix::process::CommandExt;
    use std::os::unix::process::ExitStatusExt;

    let cmd = str_arg(payload, "cmd")?;
    let mut timeout = payload
        .get("timeout_s")
        .and_then(|t| t.as_f64())
        .filter(|t| *t > 0.0)
        .unwrap_or(DEFAULT_TIMEOUT_S);
    let mut clamped = false;
    if let Some(deadline) = ctx.deadline {
        // leave the cell a moment to report the timeout itself
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .as_secs_f64()
            - 0.5;
        if remaining < timeout {
            timeout = remaining.max(0.0);
            clamped = true;
        }
    }

    let program = &ctx.shell;
    let mut command = Command::new(program);
    command
        .arg("-c")
        .arg(cmd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    if ctx.login_env {
        if let Some(env) = login_env(&ctx.login_shell) {
            command.env_clear().envs(env);
        }
    }
    #[cfg(target_os = "linux")]
    // SAFETY: prctl in the forked child before exec — async-signal-safe,
    // touches nothing of the parent.
    unsafe {
        command.pre_exec(|| {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            Ok(())
        });
    }
    if let Some(cwd) = payload.get("cwd").and_then(|c| c.as_str()) {
        command.current_dir(cwd);
    }
    if let Some(env) = payload.get("env").and_then(|e| e.as_object()) {
        for (k, v) in env {
            let val = v
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(|| crate::trace::canonical_json(v));
            command.env(k, val);
        }
    }

    let t0 = Instant::now();
    let mut child = command
        .spawn()
        .map_err(|e| fail("OSError", format!("spawn {program}: {e}")))?;
    let pid = child.id();
    LIVE.lock().unwrap().insert(pid);

    // output pumps FIRST, then stdin on its own thread (see feed_stdin)
    let out = pump(child.stdout.take().expect("piped stdout"));
    let err = pump(child.stderr.take().expect("piped stderr"));
    let stdin_text = payload
        .get("stdin")
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_string();
    let feeder = child.stdin.take().map(|si| feed_stdin(si, stdin_text));

    let limit = t0 + Duration::from_secs_f64(timeout);
    let mut timed_out = false;
    let mut interrupted = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {}
            Err(e) => return Err(fail("OSError", format!("wait {pid}: {e}"))),
        }
        if ctx.interrupt.load(Ordering::Relaxed) {
            interrupted = true;
        } else if Instant::now() >= limit {
            timed_out = true;
        }
        if timed_out || interrupted {
            kill_group(pid);
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(Duration::from_millis(POLL_MS));
    };
    // the command is over: nothing it left behind survives the call
    // (ADR-024 §1) — sweep the group, then let the pumps drain
    kill_group(pid);
    LIVE.lock().unwrap().remove(&pid);
    if let Some(stop) = feeder {
        stop.store(true, Ordering::Relaxed);
    }
    let grace_until = Instant::now() + READER_GRACE;
    let (stdout, out_trunc) = out.finish(grace_until);
    let (stderr, err_trunc) = err.finish(grace_until);

    let mut out = Map::new();
    out.insert("pid".into(), json!(pid));
    out.insert("exit".into(), json!(status.and_then(|s| s.code())));
    if let Some(sig) = status.and_then(|s| s.signal()) {
        out.insert("signal".into(), json!(sig));
    }
    out.insert("stdout".into(), json!(stdout));
    out.insert("stderr".into(), json!(stderr));
    out.insert("durationMs".into(), json!(t0.elapsed().as_millis() as i64));
    out.insert("truncated".into(), json!(out_trunc || err_trunc));
    out.insert("timedOut".into(), json!(timed_out));
    if interrupted {
        out.insert("interrupted".into(), json!(true));
    }
    if clamped {
        out.insert("timeoutClampedS".into(), json!(timeout));
    }
    Ok(Value::Object(out))
}

// --- fs.* --------------------------------------------------------------------

fn io_fail(path: &str, e: std::io::Error) -> EffectFailure {
    let type_ = match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::AlreadyExists => "FileExistsError",
        _ => "OSError",
    };
    fail(type_, format!("{path}: {e}"))
}

fn fs_read(ctx: &Ctx, payload: &Value) -> Result<Value, EffectFailure> {
    let path = str_arg(payload, "path")?;
    let encoding = payload
        .get("encoding")
        .and_then(|e| e.as_str())
        .unwrap_or("text");
    // size from metadata BEFORE any allocation: the caps bound memory,
    // not just the reply (ADR-024 §2)
    let size = std::fs::metadata(path).map_err(|e| io_fail(path, e))?.len() as usize;
    match encoding {
        // ADR-026 §4: the bytes go to the blob directory, the record
        // and the cell get a ref — no cap, nothing in memory
        "blob" => {
            let mime = payload
                .get("mime")
                .and_then(|m| m.as_str())
                .filter(|m| !m.is_empty())
                .unwrap_or("application/octet-stream");
            let r = ctx
                .blob_dir()?
                .put_path(Path::new(path), mime)
                .map_err(|e| fail("OSError", format!("{path}: {e}")))?;
            Ok(json!({"path": path, "size": size, "blob": r}))
        }
        "text" => {
            let offset = payload
                .get("offset")
                .and_then(|o| o.as_u64())
                .map(|o| o.max(1) as usize)
                .unwrap_or(1);
            let limit = payload
                .get("limit")
                .and_then(|l| l.as_u64())
                .map(|l| l as usize);
            let start = offset - 1;
            let end = limit.map_or(usize::MAX, |l| start.saturating_add(l));
            // one streamed pass: count every line, keep only the
            // selected region, and never hold more than FILE_CAP of it
            let file = std::fs::File::open(path).map_err(|e| io_fail(path, e))?;
            let mut reader = std::io::BufReader::new(file);
            let mut line = Vec::new();
            let mut selected: Vec<u8> = Vec::new();
            let mut total_lines = 0usize;
            let mut truncated = false;
            loop {
                line.clear();
                let n = reader
                    .read_until(b'\n', &mut line)
                    .map_err(|e| io_fail(path, e))?;
                if n == 0 {
                    break;
                }
                if total_lines >= start && total_lines < end {
                    let room = FILE_CAP.saturating_sub(selected.len());
                    if line.len() > room {
                        selected.extend_from_slice(&line[..room]);
                        truncated = true;
                    } else {
                        selected.extend_from_slice(&line);
                    }
                }
                total_lines += 1;
            }
            let selected = match String::from_utf8(selected) {
                Ok(s) => s,
                Err(e) => {
                    // a cut inside a multi-byte char at the cap is ours;
                    // anything else means the file isn't text
                    let valid = e.utf8_error().valid_up_to();
                    let mut bytes = e.into_bytes();
                    if truncated && valid + 4 >= bytes.len() {
                        bytes.truncate(valid);
                        String::from_utf8(bytes).unwrap_or_default()
                    } else {
                        return Err(fail(
                            "UnicodeDecodeError",
                            format!("{path}: not utf-8 — read it with encoding=\"blob\""),
                        ));
                    }
                }
            };
            Ok(json!({
                "path": path, "size": size, "lines": total_lines,
                "offset": offset, "text": selected, "truncated": truncated,
            }))
        }
        other => Err(fail(
            "TypeError",
            format!("encoding must be \"text\" or \"blob\", got {other:?}"),
        )),
    }
}

/// `*` and `?` over a single path component (a file name).
fn glob_match(pat: &str, name: &str) -> bool {
    fn rec(p: &[char], n: &[char]) -> bool {
        match (p.first(), n.first()) {
            (None, None) => true,
            (Some('*'), _) => rec(&p[1..], n) || (!n.is_empty() && rec(p, &n[1..])),
            (Some('?'), Some(_)) => rec(&p[1..], &n[1..]),
            (Some(a), Some(b)) if a == b => rec(&p[1..], &n[1..]),
            _ => false,
        }
    }
    let p: Vec<char> = pat.chars().collect();
    let n: Vec<char> = name.chars().collect();
    rec(&p, &n)
}

fn walk(
    dir: &Path,
    depth: usize,
    glob: Option<&str>,
    out: &mut Vec<Value>,
    truncated: &mut bool,
) -> Result<(), std::io::Error> {
    let mut entries: Vec<_> = std::fs::read_dir(dir)?.collect::<Result<_, _>>()?;
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        if out.len() >= LIST_CAP {
            *truncated = true;
            return Ok(());
        }
        let p: PathBuf = e.path();
        let meta = e.metadata()?;
        let ft = e.file_type()?;
        let kind = if ft.is_symlink() {
            "symlink"
        } else if meta.is_dir() {
            "dir"
        } else {
            "file"
        };
        let name = e.file_name().to_string_lossy().into_owned();
        if glob.is_none_or(|g| glob_match(g, &name)) {
            out.push(json!({
                "path": p.to_string_lossy(), "kind": kind,
                "size": if kind == "file" { Some(meta.len()) } else { None },
            }));
        }
        if kind == "dir" && depth > 1 {
            walk(&p, depth - 1, glob, out, truncated)?;
        }
    }
    Ok(())
}

fn fs_list(payload: &Value) -> Result<Value, EffectFailure> {
    let path = str_arg(payload, "path")?;
    let depth = payload
        .get("depth")
        .and_then(|d| d.as_u64())
        .map(|d| d.max(1) as usize)
        .unwrap_or(1);
    let glob = payload.get("glob").and_then(|g| g.as_str());
    let mut entries = Vec::new();
    let mut truncated = false;
    walk(Path::new(path), depth, glob, &mut entries, &mut truncated)
        .map_err(|e| io_fail(path, e))?;
    Ok(json!({"path": path, "entries": entries, "truncated": truncated}))
}

/// `content` is a str (the text, utf-8) or a raw ref (ADR-026 §4: the
/// bytes stream out of the blob directory; the record keeps the ref).
fn fs_write(ctx: &Ctx, payload: &Value) -> Result<Value, EffectFailure> {
    let path = str_arg(payload, "path")?;
    let content = payload.get("content").unwrap_or(&Value::Null);
    if !content.is_string() && !crate::blob::is_raw_ref(content) {
        return Err(fail(
            "TypeError",
            "content must be a str or a Blob (bytes → blob.from_bytes / the Blob you already hold)",
        ));
    }
    if payload
        .get("mkdirs")
        .and_then(|m| m.as_bool())
        .unwrap_or(false)
    {
        if let Some(parent) = Path::new(path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| io_fail(path, e))?;
        }
    }
    let created = !Path::new(path).exists();
    let bytes = if let Some(text) = content.as_str() {
        std::fs::write(path, text.as_bytes()).map_err(|e| io_fail(path, e))?;
        text.len() as u64
    } else {
        let hash = content["__blob"].as_str().unwrap_or("");
        let dir = ctx.blob_dir()?;
        let missing = || {
            fail(
                "blob_missing",
                format!("no blob {hash} in this bao's blob directory"),
            )
        };
        // the bytes must exist before the target is touched: a failed
        // write never truncates the file that was there
        if !dir.exists(hash) {
            return Err(missing());
        }
        let mut f = std::fs::File::create(path).map_err(|e| io_fail(path, e))?;
        dir.copy_to(hash, &mut f)
            .map_err(|e| fail("blob_missing", format!("blob {hash}: {e}")))?
            .ok_or_else(missing)?
    };
    Ok(json!({"path": path, "bytes": bytes, "created": created}))
}

fn fs_edit(payload: &Value) -> Result<Value, EffectFailure> {
    let path = str_arg(payload, "path")?;
    let old = str_arg(payload, "old")?;
    let new = str_arg(payload, "new")?;
    if old.is_empty() {
        return Err(fail("TypeError", "old must be non-empty"));
    }
    let all = payload
        .get("all")
        .and_then(|a| a.as_bool())
        .unwrap_or(false);
    let size = std::fs::metadata(path).map_err(|e| io_fail(path, e))?.len() as usize;
    if size > EDIT_CAP {
        return Err(fail(
            "fs.too_large",
            format!("{path}: {size} bytes exceeds the {EDIT_CAP}-byte edit cap — use the shell"),
        ));
    }
    let text = std::fs::read_to_string(path).map_err(|e| io_fail(path, e))?;
    let count = text.matches(old).count();
    if count == 0 {
        return Err(fail(
            "fs.edit_not_found",
            format!("{path}: old text not found — re-read the file and retry with the exact text"),
        ));
    }
    if count > 1 && !all {
        return Err(fail(
            "fs.edit_ambiguous",
            format!("{path}: old text occurs {count} times — widen it to a unique span, or pass all=True"),
        ));
    }
    let updated = if all {
        text.replace(old, new)
    } else {
        text.replacen(old, new, 1)
    };
    std::fs::write(path, updated.as_bytes()).map_err(|e| io_fail(path, e))?;
    Ok(json!({"path": path, "replacements": if all { count } else { 1 }}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> Ctx {
        Ctx {
            interrupt: Arc::new(AtomicBool::new(false)),
            deadline: None,
            shell: "/bin/sh".into(),
            login_shell: "/bin/sh".into(),
            login_env: false,
            blob_dir: None,
        }
    }

    fn ctx_with_blobs(dir: &std::path::Path) -> Ctx {
        ctx().with_blob_dir(Some(crate::blob::BlobDir::new(dir)))
    }

    #[test]
    fn commands_run_in_bash_whatever_the_login_shell_is() {
        // the tool is `bash`: heredocs, $(...), [[ ]] must work even when
        // the serve was launched from fish/zsh
        let bash = command_shell(None);
        assert!(bash.ends_with("/bash"), "{bash}");
        // an empty login PATH falls back to the system dirs (NixOS has no
        // /bin/bash: /bin/sh is the honest last resort there)
        let fallback = command_shell(Some("/nonexistent".into()));
        assert!(
            fallback.ends_with("/bash") || fallback == "/bin/sh",
            "{fallback}"
        );
        let c = Ctx {
            shell: bash,
            login_shell: "/usr/bin/fish".into(),
            ..ctx()
        };
        let out = execute(
            &c,
            "sh.run",
            &json!({"cmd": "cat <<'EOF'\nhi $BASH_VERSION\nEOF\n[[ -n $BASH_VERSION ]] && echo bashism-ok"}),
        )
        .unwrap();
        assert_eq!(out["exit"], 0, "{out}");
        let s = out["stdout"].as_str().unwrap();
        assert!(s.starts_with("hi ") && s.ends_with("bashism-ok\n"), "{s:?}");
    }

    #[test]
    fn env_snapshot_parses_after_marker_and_skips_noise() {
        let raw = format!(
            "\x1b]0;\x07profile noise\n{ENV_MARK}PATH=/a:/b\0HOME=/h\0BAD KEY=x\0=empty\0X=a=b\0"
        );
        let env = parse_env_snapshot(raw.as_bytes()).unwrap();
        assert_eq!(env["PATH"], "/a:/b");
        assert_eq!(env["HOME"], "/h");
        assert_eq!(env["X"], "a=b");
        assert_eq!(env.len(), 3);
        assert!(parse_env_snapshot(b"no marker here").is_none());
        assert!(parse_env_snapshot(ENV_MARK.as_bytes()).is_none());
    }

    #[test]
    fn login_env_snapshot_runs_once_and_has_path() {
        let env = login_env("/bin/sh").expect("sh -l should print an env");
        assert!(env.contains_key("PATH"));
        let again = login_env("/bin/sh").unwrap();
        assert!(std::ptr::eq(env, again)); // cached, one login shell per process
                                           // and commands run in it without profile noise
        let c = Ctx {
            login_env: true,
            ..ctx()
        };
        let out = execute(&c, "sh.run", &json!({"cmd": "echo $PATH"})).unwrap();
        assert_eq!(out["stdout"].as_str().unwrap().trim(), env["PATH"]);
    }

    fn run(payload: Value) -> Value {
        execute(&ctx(), "sh.run", &payload).unwrap()
    }

    #[test]
    fn run_captures_streams_and_exit() {
        let out = run(json!({"cmd": "printf 'a\\nb'; printf err >&2; exit 3"}));
        assert_eq!(out["exit"], 3);
        assert_eq!(out["stdout"], "a\nb");
        assert_eq!(out["stderr"], "err");
        assert_eq!(out["timedOut"], false);
        assert_eq!(out["truncated"], false);
        assert!(out["durationMs"].as_i64().unwrap() >= 0);
    }

    #[test]
    fn run_stdin_env_cwd() {
        let out = run(json!({"cmd": "cat", "stdin": "hi there"}));
        assert_eq!(out["stdout"], "hi there");
        let out = run(json!({"cmd": "echo $FOO", "env": {"FOO": "bar"}}));
        assert_eq!(out["stdout"], "bar\n");
        let dir = tempfile::tempdir().unwrap();
        let out = run(json!({"cmd": "pwd", "cwd": dir.path()}));
        let got = std::fs::canonicalize(out["stdout"].as_str().unwrap().trim()).unwrap();
        assert_eq!(got, std::fs::canonicalize(dir.path()).unwrap());
    }

    #[test]
    fn run_timeout_kills_group_and_returns_partial() {
        let t0 = Instant::now();
        let out = run(json!({"cmd": "echo before; sleep 5; echo after", "timeout_s": 0.3}));
        assert!(
            t0.elapsed() < Duration::from_secs(4),
            "sleep was not killed"
        );
        assert_eq!(out["timedOut"], true);
        assert_eq!(out["exit"], Value::Null);
        assert_eq!(out["stdout"], "before\n");
    }

    fn alive(pid: i32) -> bool {
        // SAFETY: signal 0 probes existence only
        unsafe { libc::kill(pid, 0) == 0 }
    }

    #[test]
    fn run_leaves_no_background_process_behind() {
        let t0 = Instant::now();
        let out = run(json!({"cmd": "sleep 30 & echo $!"}));
        assert!(
            t0.elapsed() < Duration::from_secs(5),
            "shell exit must not wait on the &-child"
        );
        let pid: i32 = out["stdout"].as_str().unwrap().trim().parse().unwrap();
        assert_eq!(out["exit"], 0);
        std::thread::sleep(Duration::from_millis(100));
        assert!(!alive(pid), "backgrounded sleep {pid} survived the call");
        let shell_pid = out["pid"].as_u64().unwrap() as u32;
        assert!(!LIVE.lock().unwrap().contains(&shell_pid)); // registry entry released
    }

    #[test]
    fn run_large_stdin_through_a_chatty_child_does_not_deadlock() {
        // cat emits while it reads: > 64 KiB each way fills both pipes
        // unless stdin is fed concurrently with the output pumps
        let big: String = (0..8000)
            .map(|i| format!("line {i:05} xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"))
            .collect();
        assert!(big.len() > 300_000);
        let t0 = Instant::now();
        let out = run(json!({"cmd": "cat", "stdin": big, "timeout_s": 20}));
        assert!(
            t0.elapsed() < Duration::from_secs(10),
            "stdin/stdout deadlock"
        );
        assert_eq!(out["exit"], 0);
        assert_eq!(out["stdout"].as_str().unwrap().len(), big.len());
        assert_eq!(out["truncated"], false);
    }

    #[test]
    fn run_capture_is_bounded() {
        let t0 = Instant::now();
        assert!(run_capture("/bin/sh", &["-c", "sleep 30"], Duration::from_millis(300)).is_none());
        assert!(t0.elapsed() < Duration::from_secs(5));
        let ok = run_capture("/bin/sh", &["-c", "printf hi"], Duration::from_secs(5)).unwrap();
        assert_eq!(ok, b"hi");
        assert!(run_capture("/bin/sh", &["-c", "exit 1"], Duration::from_secs(5)).is_none());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn pumps_stop_when_an_escaped_grandchild_holds_the_pipe() {
        // setsid puts sleep in its own session/group: killpg can't reach
        // it and it keeps our stdout pipe open — the call must still
        // return after the grace, with what was captured
        let t0 = Instant::now();
        // the 0.3 s lets the background child call setsid() before the
        // shell exits — otherwise the group kill takes it too (which is
        // the ADR's intent, but not what this test is about)
        let out = run(json!({"cmd": "setsid sleep 8 & sleep 0.3; echo hi"}));
        let took = t0.elapsed();
        assert!(
            // stops at the grace, well before the escaped sleep exits (8 s);
            // the upper bound is loose — the parallel test run loads the box
            took + Duration::from_millis(100) >= READER_GRACE && took < Duration::from_secs(6),
            "{took:?}"
        );
        assert_eq!(out["stdout"], "hi\n");
        assert_eq!(out["exit"], 0);
    }

    #[test]
    fn fs_read_streams_big_files_and_gates_by_size() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("big.txt");
        let ps = p.to_string_lossy().into_owned();
        let f = std::fs::File::create(&p).unwrap();
        let mut w = std::io::BufWriter::new(f);
        for i in 0..100_000 {
            writeln!(w, "row {i:06} ééé").unwrap(); // ~18 bytes → ~1.8 MiB
        }
        drop(w);
        let size = std::fs::metadata(&p).unwrap().len();
        assert!(size as usize > FILE_CAP);
        // a region past the cap reads fine and counts every line
        let out = execute(
            &ctx(),
            "fs.read",
            &json!({"path": ps, "offset": 99_999, "limit": 5}),
        )
        .unwrap();
        assert_eq!(out["text"], "row 099998 ééé\nrow 099999 ééé\n");
        assert_eq!(out["lines"], 100_000);
        assert_eq!(out["size"], size);
        assert_eq!(out["truncated"], false);
        // the whole file: capped, cut on a char boundary, flagged
        let out = execute(&ctx(), "fs.read", &json!({"path": ps})).unwrap();
        assert_eq!(out["truncated"], true);
        assert!(out["text"].as_str().unwrap().len() <= FILE_CAP);
        assert!(out["text"]
            .as_str()
            .unwrap()
            .starts_with("row 000000 ééé\n"));
        // bytes are not capped: the file streams into the blob
        // directory and comes back as a ref (ADR-026 §4)
        let out = execute(
            &ctx_with_blobs(dir.path()),
            "fs.read",
            &json!({"path": ps, "encoding": "blob", "mime": "text/plain"}),
        )
        .unwrap();
        assert!(crate::blob::is_raw_ref(&out["blob"]), "{out}");
        assert_eq!(out["blob"]["bytes"], size);
        assert_eq!(out["blob"]["mime"], "text/plain");
        assert_eq!(out["size"], size);
        // without a directory the byte leg fails typed
        let err = execute(&ctx(), "fs.read", &json!({"path": ps, "encoding": "blob"})).unwrap_err();
        assert_eq!(err.type_, "blob_missing");
        // edit refuses past its own cap (sparse file, no bytes written)
        let huge = dir.path().join("huge.bin");
        std::fs::File::create(&huge)
            .unwrap()
            .set_len(EDIT_CAP as u64 + 1)
            .unwrap();
        let err = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": huge.to_string_lossy(), "old": "a", "new": "b"}),
        )
        .unwrap_err();
        assert_eq!(err.type_, "fs.too_large");
    }

    #[test]
    fn run_deadline_clamps_timeout() {
        let mut c = ctx();
        c.deadline = Some(Instant::now() + Duration::from_millis(800));
        let out = execute(&c, "sh.run", &json!({"cmd": "sleep 5", "timeout_s": 60})).unwrap();
        assert_eq!(out["timedOut"], true);
        assert!(out["timeoutClampedS"].as_f64().unwrap() < 1.0);
    }

    #[test]
    fn run_interrupt_kills_child() {
        let c = ctx();
        let flag = c.interrupt.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            flag.store(true, Ordering::Relaxed);
        });
        let t0 = Instant::now();
        let out = execute(&c, "sh.run", &json!({"cmd": "sleep 5"})).unwrap();
        assert!(t0.elapsed() < Duration::from_secs(4));
        assert_eq!(out["interrupted"], true);
        assert_eq!(out["exit"], Value::Null);
    }

    #[test]
    fn run_output_is_capped_head_and_tail() {
        // 3 MiB of x's with a distinct first and last line
        let out = run(
            json!({"cmd": "echo FIRST; head -c 3000000 /dev/zero | tr '\\0' x; echo; echo LAST"}),
        );
        let s = out["stdout"].as_str().unwrap();
        assert_eq!(out["truncated"], true);
        assert!(s.starts_with("FIRST\n"));
        assert!(s.ends_with("LAST\n"));
        assert!(s.contains("bytes elided"));
        assert!(s.len() < STREAM_CAP + 100);
    }

    #[test]
    fn spawn_poll_kill_not_in_this_build() {
        let err = execute(&ctx(), "sh.spawn", &json!({"cmd": "true"})).unwrap_err();
        assert_eq!(err.type_, "NotImplementedError");
    }

    #[test]
    fn redact_masks_credential_looking_env_only_for_sh() {
        let p = json!({"cmd": "x", "env": {"API_KEY": "k", "GITHUB_TOKEN": "t", "PATH": "/bin"}});
        let r = redact_input("sh.run", &p);
        assert_eq!(r["env"]["API_KEY"], "***");
        assert_eq!(r["env"]["GITHUB_TOKEN"], "***");
        assert_eq!(r["env"]["PATH"], "/bin");
        assert_eq!(r["cmd"], "x");
        let f = json!({"path": "KEY"});
        assert_eq!(redact_input("fs.read", &f), f);
    }

    #[test]
    fn classify_and_runtime_value() {
        assert_eq!(classify("sh.run"), "mutate");
        assert_eq!(classify("fs.read"), "read");
        assert_eq!(classify("fs.list"), "read");
        assert_eq!(classify("fs.write"), "mutate");
        assert_eq!(classify("fs.edit"), "mutate");
        let rv = runtime_value();
        assert!(rv["cwd"].is_string());
        assert_eq!(rv["os"], std::env::consts::OS);
    }

    #[test]
    fn fs_write_read_roundtrip_with_offset_limit() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("sub/a.txt");
        let ps = p.to_string_lossy().into_owned();
        let err = execute(&ctx(), "fs.write", &json!({"path": ps, "content": "x"})).unwrap_err();
        assert_eq!(err.type_, "FileNotFoundError");
        let out = execute(
            &ctx(),
            "fs.write",
            &json!({"path": ps, "content": "l1\nl2\nl3\nl4\n", "mkdirs": true}),
        )
        .unwrap();
        assert_eq!(out["created"], true);
        assert_eq!(out["bytes"], 12);
        let out = execute(&ctx(), "fs.read", &json!({"path": ps})).unwrap();
        assert_eq!(out["text"], "l1\nl2\nl3\nl4\n");
        assert_eq!(out["lines"], 4);
        let out = execute(
            &ctx(),
            "fs.read",
            &json!({"path": ps, "offset": 2, "limit": 2}),
        )
        .unwrap();
        assert_eq!(out["text"], "l2\nl3\n");
        let out = execute(&ctx(), "fs.write", &json!({"path": ps, "content": "new"})).unwrap();
        assert_eq!(out["created"], false);
    }

    /// ADR-026 §4: a binary file reads as a Blob ref (the bytes land in
    /// the directory under their hash, mime from the caller), and a ref
    /// as `content` writes those bytes back out; a str is text; anything
    /// else is refused; a ref with no file is `blob_missing`.
    #[test]
    fn fs_read_binary_is_a_blob_and_write_takes_one() {
        let dir = tempfile::tempdir().unwrap();
        let c = ctx_with_blobs(dir.path());
        let p = dir.path().join("b.bin");
        let bytes = [0u8, 159, 146, 150];
        std::fs::write(&p, bytes).unwrap();
        let ps = p.to_string_lossy().into_owned();
        let err = execute(&c, "fs.read", &json!({"path": ps})).unwrap_err();
        assert_eq!(err.type_, "UnicodeDecodeError");
        assert!(err.message.contains("encoding=\"blob\""));
        let out = execute(
            &c,
            "fs.read",
            &json!({"path": ps, "encoding": "blob", "mime": "image/png"}),
        )
        .unwrap();
        let r = &out["blob"];
        assert!(crate::blob::is_raw_ref(r), "{out}");
        assert_eq!(r["__blob"], crate::blob::hash_of(&bytes));
        assert_eq!(r["bytes"], 4);
        assert_eq!(r["mime"], "image/png");
        assert_eq!(out["size"], 4);
        assert_eq!(
            c.blob_dir
                .as_ref()
                .unwrap()
                .read(r["__blob"].as_str().unwrap())
                .unwrap()
                .unwrap(),
            bytes
        );
        // default mime when the caller gives none
        let out = execute(&c, "fs.read", &json!({"path": ps, "encoding": "blob"})).unwrap();
        assert_eq!(out["blob"]["mime"], "application/octet-stream");
        // the ref writes the bytes back out
        let q = dir.path().join("sub/copy.bin");
        let qs = q.to_string_lossy().into_owned();
        let out = execute(
            &c,
            "fs.write",
            &json!({"path": qs, "content": r, "mkdirs": true}),
        )
        .unwrap();
        assert_eq!(out["bytes"], 4);
        assert_eq!(out["created"], true);
        assert_eq!(std::fs::read(&q).unwrap(), bytes);
        // not a str, not a ref → refused before touching the path
        let err = execute(&c, "fs.write", &json!({"path": qs, "content": 42})).unwrap_err();
        assert_eq!(err.type_, "TypeError");
        // a ref whose bytes are gone → typed
        let gone = crate::blob::raw_ref(&crate::blob::hash_of(b"nope"), 4, "text/plain");
        let err = execute(&c, "fs.write", &json!({"path": qs, "content": gone})).unwrap_err();
        assert_eq!(err.type_, "blob_missing");
        // and no directory at all → typed, the file untouched
        let err = execute(&ctx(), "fs.write", &json!({"path": qs, "content": r})).unwrap_err();
        assert_eq!(err.type_, "blob_missing");
    }

    #[test]
    fn fs_edit_exact_once_or_all() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("e.txt");
        std::fs::write(&p, "foo bar foo\n").unwrap();
        let ps = p.to_string_lossy().into_owned();
        let err = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": ps, "old": "nope", "new": "x"}),
        )
        .unwrap_err();
        assert_eq!(err.type_, "fs.edit_not_found");
        let err = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": ps, "old": "foo", "new": "x"}),
        )
        .unwrap_err();
        assert_eq!(err.type_, "fs.edit_ambiguous");
        assert!(err.message.contains("2 times"));
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "foo bar foo\n"); // no write
        let out = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": ps, "old": "bar", "new": "baz"}),
        )
        .unwrap();
        assert_eq!(out["replacements"], 1);
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "foo baz foo\n");
        let out = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": ps, "old": "foo", "new": "q", "all": true}),
        )
        .unwrap();
        assert_eq!(out["replacements"], 2);
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "q baz q\n");
        let err = execute(
            &ctx(),
            "fs.edit",
            &json!({"path": ps, "old": "", "new": "q"}),
        )
        .unwrap_err();
        assert_eq!(err.type_, "TypeError");
    }

    #[test]
    fn fs_list_glob_and_depth() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("d")).unwrap();
        std::fs::write(dir.path().join("a.rs"), "1").unwrap();
        std::fs::write(dir.path().join("b.txt"), "22").unwrap();
        std::fs::write(dir.path().join("d/c.rs"), "333").unwrap();
        let root = dir.path().to_string_lossy().into_owned();
        let out = execute(&ctx(), "fs.list", &json!({"path": root})).unwrap();
        let names: Vec<&str> = out["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["path"].as_str().unwrap().rsplit('/').next().unwrap())
            .collect();
        assert_eq!(names, ["a.rs", "b.txt", "d"]);
        assert_eq!(out["entries"][2]["kind"], "dir");
        assert_eq!(out["entries"][1]["size"], 2);
        let out = execute(
            &ctx(),
            "fs.list",
            &json!({"path": root, "glob": "*.rs", "depth": 3}),
        )
        .unwrap();
        let names: Vec<&str> = out["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["path"].as_str().unwrap().rsplit('/').next().unwrap())
            .collect();
        assert_eq!(names, ["a.rs", "c.rs"]);
        assert!(glob_match("a?c*", "abcdef"));
        assert!(!glob_match("*.rs", "x.py"));
    }

    #[test]
    fn capture_keeps_everything_under_cap() {
        let mut c = Capture::new(10);
        c.push(b"0123456789");
        assert_eq!(c.total(), 10);
        assert_eq!(c.render(), ("0123456789".into(), false));
        c.push(b"AB");
        assert_eq!(c.total(), 12);
        let (s, t) = c.render();
        assert!(t);
        assert!(s.starts_with("01234") && s.ends_with("789AB"));
    }
}
