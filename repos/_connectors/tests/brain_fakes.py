"""A fake of Bao's space for the brain program tests, modelling the server
rules the live runs taught (ADR-033 §2, §4, §6): the hub's stores are
SHARED (reads and id filters use "<objectId>/<recordId>", writes the plain
id), a refused record write is HTTP 200 with `rejections`, and a
write-once field rejects a changed value. Objects carry one type, any
collections and xKey-nested property groups, like normalized rows. A type
exists only once something installed it, and a query naming a type the
space lacks raises, as any@v1 does: the mailbox type arrives with the first
mail sync."""

import copy

HUB = "hub"
SHARED_PREFIX = "hubtype_"     # collections of the hub's shared stores
DATASETS = {k: f"{SHARED_PREFIX}{k}" for k in
            ("signals", "config", "incidents", "observations", "promises", "facts",
             "cards", "drafts", "approvals", "standups", "sends", "asks")}
ANCHOR = "trigger-anchor"      # the bao/triggers/v1 bundle child
# what a set-up brain's space has before any mail sync: the built-in page,
# the people and tasks catalog apps' types, the hub
BASE_TYPES = ("page", "profile", "task", "brain_hub")
WRITE_ONCE = {"promises": {"text", "quote", "source", "mailboxId", "channel", "madeAt",
                           "dueAt", "personEmail"},
              "asks": {"senderIdentity", "spaceId", "chatId", "kind", "text", "quote",
                       "source", "askedAt", "suggestedDeadline"},
              "cards": {"kind", "title", "reason", "subjectKey", "items"}}


def _key_of(dataset):
    return dataset[len(SHARED_PREFIX):] if dataset.startswith(SHARED_PREFIX) else dataset


def _get(row, path):
    cur = row
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _instant(value):
    # an instant reads and compares as {"$date": ms}
    return value["$date"] if isinstance(value, dict) and set(value) == {"$date"} else value


_ORDER = {"$gt": lambda v, a: v > a, "$gte": lambda v, a: v >= a,
          "$lt": lambda v, a: v < a, "$lte": lambda v, a: v <= a}


def _matches(value, cond):
    value = _instant(value)
    if isinstance(cond, dict) and set(cond) == {"$date"}:
        cond = cond["$date"]
    if isinstance(cond, dict):
        cond = {op: _instant(arg) for op, arg in cond.items()}
        for op, arg in cond.items():
            if op in _ORDER and not (value is not None and _ORDER[op](value, arg)):
                return False
            if op == "$in" and not (any(v in arg for v in value) if isinstance(value, list)
                                    else value in arg):
                return False
            if op == "$nin" and (any(v in arg for v in value) if isinstance(value, list)
                                 else value in arg):
                return False
        return True
    if cond is None:
        return value is None
    if isinstance(value, list):
        return cond in value
    return value == cond


class FakeAnyError(Exception):
    """any@v1's AnyError: the server answered 4xx/5xx."""


class FakeSpace:
    AnyError = FakeAnyError

    def __init__(self, space="bao-space"):
        self.space = space
        self.objects = {}          # id -> row
        self.records = {}          # (oid, dataset) -> {rid: record}
        self.search_hits = {}      # scope -> [hit]
        self.search_fails = set()  # scopes the index refuses
        self.refuse = {}           # record id -> why the server refuses it: a reason,
                                   # or {code, reason}
        self.calls = []
        self.types = set(BASE_TYPES)
        self.chat = []             # chat_send bodies, in order
        self.skills = {}           # name -> markdown (get_skill)
        self._n = 0

    # --- seeding -----------------------------------------------------------
    def add_object(self, oid, type_, collections=(), **groups):
        self.types.add(type_)
        row = {"id": oid, "any": {"type": type_, "collections": list(collections),
                                  "name": groups.pop("name", oid)}}
        row.update(copy.deepcopy(groups))
        self.objects[oid] = row
        return row

    def add_mailbox(self, oid, address):
        return self.add_object(oid, "mailbox", mailbox={"address": address}, name=address)

    def add_mail(self, mailbox, mid, **fields):
        self.records.setdefault((mailbox, "email_messages"), {})[mid] = {"id": mid, **fields}

    def seed_lists(self):
        """The exclusion list and blocklist a set-up brain starts with: empty."""
        self.set_config("exclusions", {"version": 1, "personIds": [], "emails": [],
                                       "domains": []})
        self.set_config("blocklist", {"senders": [], "domains": [], "labels": []})

    def set_config(self, name, value):
        self.records.setdefault((HUB, DATASETS["config"]), {})[name] = {"id": name,
                                                                        "value": value}

    def config(self, name):
        rec = self.records.get((HUB, DATASETS["config"]), {}).get(name)
        return rec and rec["value"]

    def store(self, key):
        return self.records.get((HUB, DATASETS.get(key, f"{SHARED_PREFIX}{key}")), {})

    # --- chats (ADR-039) ---------------------------------------------------
    # spaces the account has, their members, the one general chat each, and
    # the account's identity directory; a chat message is a record on the
    # chat object's chat_messages, ordered by _ver.id
    me_identity = "me-id"
    me_name = "Me"

    def add_space(self, sid, name=None, role="writer", one_to_one=False, status="active",
                  members=()):
        self.spaces = getattr(self, "spaces", {})
        self.spaces[sid] = {"id": sid, "name": name or sid, "status": status,
                            "ownRole": role,
                            "spaceType": "any.onetoone" if one_to_one else "any.space"}
        self.members = getattr(self, "members", {})
        self.members[sid] = [{"identity": self.me_identity, "name": self.me_name,
                              "permission": role, "status": "active"}] + [
            {"identity": i, "name": n, "permission": "writer", "status": "active"}
            for i, n in members]
        return f"chat-{sid}"

    def add_message(self, sid, mid, creator, text, ver, at_s, **extra):
        rec = {"id": mid, "creator": creator, "text": text, "_ver": {"id": ver},
               "createdAt": {"$date": int(at_s * 1000)}, **extra}
        self.records.setdefault((f"chat-{sid}", "chat_messages"), {})[mid] = rec
        return rec

    def me(self):
        self.calls.append(("me",))
        return {"identity": self.me_identity, "name": self.me_name}

    def list_spaces(self, raw=False):
        return [dict(r) for r in getattr(self, "spaces", {}).values()]

    def list_members(self, space):
        self.calls.append(("list_members", space))
        return [dict(m) for m in getattr(self, "members", {}).get(space, [])]

    def general_chat(self, space):
        return f"chat-{space}"

    def get_identity(self, identity):
        self.calls.append(("get_identity", identity))
        name = getattr(self, "directory", {}).get(identity)
        return {"identity": identity, "name": name} if name else None

    def triggers(self):
        return self.records.get((ANCHOR, "agent_triggers"), {})

    # --- any@v1 ------------------------------------------------------------
    def bao_space(self):
        return self.space

    def bundle_child(self, space, bundle_id, seed, type_key=None, **_):
        assert (bundle_id, seed) == ("bao/v1", "bao/triggers/v1"), (bundle_id, seed)
        return {"objectId": ANCHOR}

    def general_chat(self, space):
        return "general-chat"

    def chat_send(self, space, chat_id, body):
        self.calls.append(("chat_send", chat_id))
        self.chat.append(copy.deepcopy(body))
        return {"id": f"msg{len(self.chat)}"}

    def get_skill(self, name):
        if name not in self.skills:
            raise LookupError(f"no skill named {name!r}")
        return self.skills[name]

    def list_types(self, space):
        return [{"id": f"type-{t}", "xKey": t} for t in sorted(self.types)]

    def query_objects(self, space, normalize=True, filter=None, sort=None, limit=None,
                      offset=0, **_):
        wanted = (filter or {}).get("any.type")
        known = {t["xKey"] for t in self.list_types(space)}
        if isinstance(wanted, str) and wanted not in known:
            raise ValueError(f'type "{wanted}" doesn\'t exist')
        out = []
        for row in self.objects.values():
            ok = True
            for path, cond in (filter or {}).items():
                if path == "any.type":
                    ok = ok and row["any"]["type"] == cond
                elif path == "any.collections":
                    have = row["any"]["collections"]
                    if isinstance(cond, dict) and set(cond) == {"$nin"}:
                        ok = ok and not any(c in have for c in cond["$nin"])
                    else:
                        ok = ok and cond in have
                else:
                    ok = ok and _matches(_get(row, path), cond)
            if ok:
                out.append(copy.deepcopy(row))
        for key in reversed(sort or []):
            path = key.lstrip("-")
            out.sort(key=lambda r, p=path: _instant(_get(r, p)) or 0, reverse=key.startswith("-"))
        out = out[offset:]
        return out[:limit] if limit else out

    def query(self, space, oid, ds, filter=None, sort=None, limit=None, **_):
        self.calls.append(("query", oid, _key_of(ds)))
        shared = ds.startswith(SHARED_PREFIX)
        rows = [({**r, "id": f"{oid}/{r['id']}"} if shared else dict(r))
                for r in self.records.get((oid, ds), {}).values()]
        for path, cond in (filter or {}).items():
            rows = [r for r in rows if _matches(_get(r, path), cond)]
        for key in reversed(sort or []):
            desc = key.startswith("-")
            name = key.lstrip("-")
            rows.sort(key=lambda r, n=name: str(_get(r, n)) if isinstance(_get(r, n), dict)
                      else (_get(r, n) or 0), reverse=desc)
        return rows[:limit] if limit else rows

    def search(self, space, query, scopes=None, limit=None, **_):
        self.calls.append(("search", query, tuple(scopes or ()), limit))
        hits = []
        for scope in scopes or sorted(self.search_hits):
            if scope in self.search_fails:
                raise FakeAnyError(f"400 search.unknown_scope {scope}")
            hits += [{"scope": scope, **h} for h in self.search_hits.get(scope, [])]
        return {"hits": copy.deepcopy(hits[:limit] if limit else hits)}

    def upsert_record(self, space, oid, ds, rid, value):
        self.calls.append(("upsert_record", _key_of(ds), rid))
        self.records.setdefault((oid, ds), {})[rid] = {"id": rid, **copy.deepcopy(value)}
        return {"recordIds": [rid]}

    def upsert_records(self, space, oid, ds, records, page_size=None):
        store = self.records.setdefault((oid, ds), {})
        once = WRITE_ONCE.get(_key_of(ds), set())
        out = {"created": 0, "updated": 0, "skipped": 0, "rejections": []}
        for i, rec in enumerate(records):
            if rec["id"] in self.refuse:
                why = self.refuse[rec["id"]]
                why = why if isinstance(why, dict) else {"reason": why}
                out["rejections"].append({"index": i, "id": rec["id"], **why})
                continue
            have = store.get(rec["id"])
            if have is None:
                store[rec["id"]] = {"id": rec["id"], **copy.deepcopy(rec["fields"])}
                out["created"] += 1
                continue
            changed = {k for k, v in rec["fields"].items() if have.get(k) != v}
            if changed & once:
                out["rejections"].append({"index": i, "id": rec["id"],
                                          "reason": f"write-once {sorted(changed & once)}"})
            elif changed:
                # the server writes a changed mutable field over the stored one
                have.update({k: copy.deepcopy(rec["fields"][k]) for k in changed})
                out["updated"] += 1
            else:
                out["skipped"] += 1
        self.calls.append(("upsert_records", _key_of(ds), len(records)))
        return out

    def modify(self, space, body):
        store = self.records.setdefault((body["objectId"], body["dataset"]), {})
        for r in body["records"]:
            assert "/" not in r["id"], "writes take the plain id"
            for op in r["ops"]:
                assert op["type"] == "$set"
                store[r["id"]][op["path"]] = op["value"]
            self.calls.append(("modify", _key_of(body["dataset"]), r["id"],
                               [op["path"] for op in r["ops"]]))
        return {"recordIds": [r["id"] for r in body["records"]]}

    def create_object(self, space, body, **_):
        self._n += 1
        oid = f"obj{self._n}"
        groups = copy.deepcopy(body.get("initialProperties") or {})
        self.add_object(oid, body.get("type", "page"), body.get("collections") or [],
                        name=body.get("name"), **groups)
        self.calls.append(("create_object", body.get("type"), oid))
        return {"objectId": oid}

    def update_object(self, space, oid, body, **_):
        for group, values in body.items():
            self.objects[oid].setdefault(group, {}).update(copy.deepcopy(values))
        self.calls.append(("update_object", oid, sorted(body)))
        return {"objectId": oid}

    def add_to_collection(self, space, oid, collection):
        cols = self.objects[oid]["any"]["collections"]
        if collection not in cols:
            cols.append(collection)
        self.calls.append(("add_to_collection", oid, collection))
        return {}


def job_args(space):
    return {"space": space.space, "hub": HUB, "datasets": dict(DATASETS)}
