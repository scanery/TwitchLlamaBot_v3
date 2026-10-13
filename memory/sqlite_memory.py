# -*- coding: utf-8 -*-
import json
import os
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path


class SQLiteMemory:
    """Memória persistente local do bot.

    A memória é separada por função:
    - events: histórico episódico bruto, com retenção ampla;
    - people: perfil resumido dos espectadores conhecidos;
    - memories: fatos/conhecimentos consolidados;
    - streamer: informações persistentes sobre o streamer/canal;
    - meta: controle da migração dos JSONL antigos.
    """

    def __init__(self, db_path=None, channel=None):
        self.channel = (channel or os.getenv("TWITCH_CHANNEL", "soraakando")).lower().strip()
        self.db_path = Path(db_path or f"bot_memory_{self.channel}.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Todas as operações SQLite do processo passam por esta trava.
        # O bot possui threads de áudio, chat, reflexão e scheduler.
        # Serializar o acesso evita corrida de conexões/PRAGMA no Windows
        # e impede o encerramento de ficar preso esperando SQLite.
        self._db_lock = threading.RLock()

        self._init_db()
        self._migrate_legacy_jsonl()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=8,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self):
        """Abre uma conexão SQLite com acesso serializado no processo."""
        with self._db_lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self):
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    username TEXT,
                    text TEXT NOT NULL,
                    session_id TEXT,
                    processed INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_processed_ts ON events(processed, ts);
                CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(username, ts);

                CREATE TABLE IF NOT EXISTS people (
                    username TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    interactions INTEGER NOT NULL DEFAULT 0,
                    familiarity REAL NOT NULL DEFAULT 0.0,
                    profile_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    last_recalled REAL,
                    recall_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject, active);
                CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type, active);

                CREATE TABLE IF NOT EXISTS streamer_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    event_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def _get_meta(self, key):
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def _set_meta(self, key, value):
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _migrate_legacy_jsonl(self):
        if self._get_meta("legacy_jsonl_migrated") == "1":
            return

        files = [
            (f"mem_curto_{self.channel}.jsonl", "legacy_curto"),
            (f"mem_medio_{self.channel}.jsonl", "legacy_medio"),
            (f"mem_longo_{self.channel}.jsonl", "legacy_longo"),
        ]

        migrated = 0
        with self._connection() as conn:
            for filename, legacy_type in files:
                path = Path(filename)
                if not path.exists():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue

                            ts = float(obj.get("timestamp", time.time()))
                            author = str(obj.get("autor") or obj.get("usuario") or "").strip()
                            text = str(obj.get("texto") or obj.get("mensagem") or "").strip()
                            if not text:
                                continue

                            conn.execute(
                                "INSERT INTO events(ts, event_type, username, text, processed) VALUES(?, ?, ?, ?, 1)",
                                (ts, legacy_type, author, text),
                            )
                            migrated += 1
                except Exception:
                    continue

        self._set_meta("legacy_jsonl_migrated", "1")
        self._set_meta("legacy_jsonl_migrated_count", migrated)

    def start_session(self, session_id):
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(id, started_at) VALUES(?, ?)",
                (session_id, time.time()),
            )

    def end_session(self, session_id):
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, event_count = (SELECT COUNT(*) FROM events WHERE session_id = ?) WHERE id = ?",
                (time.time(), session_id, session_id),
            )

    def add_event(self, event_type, text, username=None, timestamp=None, session_id=None):
        text = str(text or "").strip()
        if not text:
            return None

        ts = float(timestamp or time.time())
        username = (str(username).strip().lower() if username else None)

        with self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO events(ts, event_type, username, text, session_id, processed) VALUES(?, ?, ?, ?, ?, 0)",
                (ts, event_type, username, text, session_id),
            )
            event_id = cur.lastrowid

            if username and event_type == "chat":
                row = conn.execute("SELECT * FROM people WHERE username = ?", (username,)).fetchone()
                now = time.time()
                if row:
                    interactions = int(row["interactions"]) + 1
                    familiarity = min(1.0, float(row["familiarity"]) + 0.025)
                    conn.execute(
                        "UPDATE people SET last_seen=?, interactions=?, familiarity=? WHERE username=?",
                        (now, interactions, familiarity, username),
                    )
                else:
                    conn.execute(
                        "INSERT INTO people(username, first_seen, last_seen, interactions, familiarity, profile_json) VALUES(?, ?, ?, 1, 0.05, '{}')",
                        (username, now, now),
                    )

                if session_id:
                    conn.execute("UPDATE sessions SET event_count = event_count + 1 WHERE id = ?", (session_id,))

        return event_id

    def get_unprocessed_events(self, limit=400, before_ts=None):
        query = "SELECT id, ts, event_type, username, text, session_id FROM events WHERE processed = 0"
        args = []
        if before_ts is not None:
            query += " AND ts <= ?"
            args.append(float(before_ts))
        query += " ORDER BY ts ASC LIMIT ?"
        args.append(int(limit))

        with self._connection() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def mark_processed(self, event_ids):
        ids = [int(x) for x in event_ids if x is not None]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as conn:
            conn.execute(f"UPDATE events SET processed = 1 WHERE id IN ({placeholders})", ids)

    def get_recent_people(self, usernames=None, limit=20):
        with self._connection() as conn:
            if usernames:
                names = [str(x).lower() for x in usernames if x]
                placeholders = ",".join("?" for _ in names)
                rows = conn.execute(
                    f"SELECT * FROM people WHERE username IN ({placeholders}) ORDER BY last_seen DESC LIMIT ?",
                    [*names, int(limit)],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM people ORDER BY last_seen DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            try:
                item["profile"] = json.loads(item.pop("profile_json") or "{}")
            except Exception:
                item["profile"] = {}
            result.append(item)
        return result

    def update_person(self, username, updates):
        username = str(username or "").strip().lower()
        if not username or not isinstance(updates, dict):
            return

        now = time.time()
        with self._connection() as conn:
            row = conn.execute("SELECT profile_json FROM people WHERE username = ?", (username,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO people(username, first_seen, last_seen, interactions, familiarity, profile_json) VALUES(?, ?, ?, 0, 0.0, ?)",
                    (username, now, now, json.dumps(updates, ensure_ascii=False)),
                )
                return

            try:
                profile = json.loads(row["profile_json"] or "{}")
            except Exception:
                profile = {}
            self._deep_merge(profile, updates)
            conn.execute(
                "UPDATE people SET last_seen=?, profile_json=? WHERE username=?",
                (now, json.dumps(profile, ensure_ascii=False), username),
            )

    @staticmethod
    def _deep_merge(target, updates):
        for key, value in updates.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                SQLiteMemory._deep_merge(target[key], value)
            elif isinstance(value, list):
                existing = target.get(key)
                if not isinstance(existing, list):
                    existing = []
                for item in value:
                    if item not in existing:
                        existing.append(item)
                target[key] = existing[-20:]
            else:
                target[key] = value

    def add_memory(self, memory_type, subject, content, importance=0.5, confidence=0.5):
        subject = str(subject or "").strip().lower()
        content = str(content or "").strip()
        if not content:
            return None

        importance = max(0.0, min(1.0, float(importance)))
        confidence = max(0.0, min(1.0, float(confidence)))
        now = time.time()

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT id, importance, confidence FROM memories WHERE memory_type=? AND subject=? AND content=? AND active=1",
                (memory_type, subject, content),
            ).fetchone()
            if existing:
                new_importance = max(float(existing["importance"]), importance)
                new_confidence = max(float(existing["confidence"]), confidence)
                conn.execute(
                    "UPDATE memories SET importance=?, confidence=?, last_recalled=? WHERE id=?",
                    (new_importance, new_confidence, now, existing["id"]),
                )
                return int(existing["id"])

            cur = conn.execute(
                "INSERT INTO memories(memory_type, subject, content, importance, confidence, created_at, last_recalled) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (memory_type, subject, content, importance, confidence, now, now),
            )
            return cur.lastrowid

    def search_memories(self, query="", subjects=None, limit=12):
        query = str(query or "").strip().lower()
        subjects = [str(x).lower() for x in (subjects or []) if x]

        with self._connection() as conn:
            candidates = conn.execute(
                "SELECT * FROM memories WHERE active=1 ORDER BY importance DESC, COALESCE(last_recalled, created_at) DESC LIMIT 500"
            ).fetchall()

        def score(row):
            text = f"{row['subject']} {row['content']}".lower()
            s = float(row["importance"]) * 3.0 + float(row["confidence"]) * 2.0
            if query:
                for token in query.split():
                    if token in text:
                        s += 2.0
            for subject in subjects:
                if subject and subject in text:
                    s += 4.0
            if row["last_recalled"]:
                age_days = max(0.0, (time.time() - float(row["last_recalled"])) / 86400.0)
                s += max(0.0, 1.5 - age_days * 0.02)
            return s

        selected = sorted(candidates, key=score, reverse=True)[: int(limit)]
        now = time.time()
        with self._connection() as conn:
            for row in selected:
                conn.execute(
                    "UPDATE memories SET last_recalled=?, recall_count=recall_count+1 WHERE id=?",
                    (now, row["id"]),
                )
        return [dict(row) for row in selected]

    def set_streamer_profile(self, profile):
        if not isinstance(profile, dict):
            return
        now = time.time()
        with self._connection() as conn:
            conn.execute("DELETE FROM streamer_profile")
            for key, value in profile.items():
                conn.execute(
                    "INSERT INTO streamer_profile(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (str(key), json.dumps(value, ensure_ascii=False), now),
                )

    def get_streamer_profile(self):
        with self._connection() as conn:
            rows = conn.execute("SELECT key, value FROM streamer_profile").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except Exception:
                result[row["key"]] = row["value"]
        return result

    _CHAVES_STREAMER = ("display_name", "description", "broadcaster_type", "language")

    def build_context(self, current_events, max_people=10, max_memories=12, max_chars=3500):
        usernames = []
        for event in current_events:
            username = event.get("username") or event.get("usuario")
            if username and username.lower() not in usernames:
                usernames.append(username.lower())

        people = self.get_recent_people(usernames=usernames, limit=max_people) if usernames else []
        memories = self.search_memories(subjects=["streamer", "canal"] + usernames[:8], limit=max_memories)
        episodes = self.recent_episodes(limit=4)
        streamer = self.get_streamer_profile()

        partes = []

        linhas = []
        for chave in self._CHAVES_STREAMER:
            valor = streamer.get(chave)
            if valor:
                linhas.append(f"{chave}: {valor}")
        if linhas:
            partes.append("STREAMER:\n" + "\n".join(linhas))

        if episodes:
            partes.append("O QUE ACONTECEU ANTES (do mais antigo ao mais novo):\n" + "\n".join(f"- {e}" for e in episodes))

        conhecidos = []
        for person in people:
            perfil = person.get("profile") or {}
            fatos = "; ".join(perfil.get("fatos", [])[:4])
            inter = ", ".join(perfil.get("interesses", [])[:4])
            extra = " | ".join(x for x in (fatos, f"curte: {inter}" if inter else "") if x)
            conhecidos.append(
                f"- {person['username']} ({person['interactions']} msgs, familiaridade {person['familiarity']:.1f})"
                + (f": {extra}" if extra else "")
            )
        if conhecidos:
            partes.append("PESSOAS DO CHAT QUE VOCÊ CONHECE:\n" + "\n".join(conhecidos))

        memorias = [
            f"- [{m['memory_type']}] {m['subject']}: {m['content']}"
            for m in memories if m["memory_type"] != "episodio"
        ]
        if memorias:
            partes.append("COISAS QUE VOCÊ LEMBRA:\n" + "\n".join(memorias))

        if not partes:
            return "Sem memória relevante ainda."

        texto = "\n\n".join(partes)
        return texto[:max_chars]

    def recent_episodes(self, limit=4):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT content FROM memories WHERE memory_type='episodio' AND active=1 "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [r["content"] for r in reversed(rows)]

    def recent_chat_texts(self, limit=14, exclude=()):
        """Mensagens reais recentes do chat, para o bot imitar o jeito de escrever."""
        excl = {str(x).lower() for x in exclude}
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT username, text FROM events WHERE event_type='chat' ORDER BY ts DESC LIMIT 200"
            ).fetchall()
        saida, por_usuario = [], {}
        for r in rows:
            u = (r["username"] or "").lower()
            t = (r["text"] or "").strip()
            if u in excl or not (3 <= len(t) <= 90) or t.startswith(("@", "!")):
                continue
            if por_usuario.get(u, 0) >= 2:
                continue
            por_usuario[u] = por_usuario.get(u, 0) + 1
            saida.append(t)
            if len(saida) >= limit:
                break
        return list(reversed(saida))

    def emote_candidates(self, limit=6):
        """Palavras que parecem emotes: repetidas por 2+ pessoas, com cara de emote."""
        import re
        from collections import Counter, defaultdict
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT username, text FROM events WHERE event_type='chat' ORDER BY ts DESC LIMIT 400"
            ).fetchall()
        cont, quem = Counter(), defaultdict(set)
        for r in rows:
            for tok in set((r["text"] or "").split()):
                tok = tok.strip(".,!?")
                if len(tok) < 3 or len(tok) > 20 or tok.startswith("@"):
                    continue
                tem_maiuscula_no_meio = bool(re.search(r"[a-z0-9][A-Z]", tok))
                caps_curta = tok.isupper() and tok.isalpha() and len(tok) <= 6
                if tem_maiuscula_no_meio or caps_curta:
                    cont[tok] += 1
                    quem[tok].add(r["username"])
        return [t for t, n in cont.most_common(30) if n >= 3 and len(quem[t]) >= 2][:limit]

    def events_since(self, ts, event_types=("chat", "audio", "visao")):
        marcas = ",".join("?" for _ in event_types)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT id, ts, event_type, username, text FROM events WHERE ts >= ? AND event_type IN ({marcas}) ORDER BY ts ASC",
                [float(ts), *event_types],
            ).fetchall()
        return [dict(r) for r in rows]

    def add_bot_message(self, text, session_id=None):
        """Registra o que o bot falou (ou falaria). Não entra na janela de reflexão."""
        event_id = self.add_event("bot", text, username=None, session_id=session_id)
        if event_id:
            self.mark_processed([event_id])
        return event_id

    def recent_bot_messages(self, limit=6):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT text FROM events WHERE event_type='bot' ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def stats(self):
        with self._connection() as conn:
            def n(sql):
                return conn.execute(sql).fetchone()[0]
            return {
                "eventos": n("SELECT COUNT(*) FROM events"),
                "pendentes": n("SELECT COUNT(*) FROM events WHERE processed=0 AND event_type!='bot'"),
                "pessoas": n("SELECT COUNT(*) FROM people"),
                "memorias": n("SELECT COUNT(*) FROM memories WHERE active=1 AND memory_type!='episodio'"),
                "episodios": n("SELECT COUNT(*) FROM memories WHERE active=1 AND memory_type='episodio'"),
            }

    def top_memories(self, limit=12):
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT memory_type, subject, content, importance, confidence FROM memories "
                "WHERE active=1 AND memory_type!='episodio' ORDER BY importance DESC, created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_events(self):
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM events").fetchone()
        return int(row["total"] or 0)

    def cleanup_old_events(self, days=30):
        cutoff = time.time() - (days * 86400)
        with self._connection() as conn:
            conn.execute("DELETE FROM events WHERE processed=1 AND ts < ?", (cutoff,))
