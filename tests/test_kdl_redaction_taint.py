"""Exit-condition 7: ingest redaction + taint propagation + at-rest encryption.

* Content containing secrets (sk-/high-entropy/AKSK), ``password=`` style creds,
  phone numbers, etc. is redacted at ingest: the persisted body is redacted and
  ``body_redacted=True``.
* Taint is upgraded to the strictest of {redaction, declared, every quote
  taint}; SENSITIVE never washes down to CLEAN.
* Plaintext bodies/quotes are NEVER persisted: only AES-GCM cipher BLOBs land in
  the DB (the plaintext-grep=0 exit criterion §3.5).
* SENSITIVE KUs are excluded from external_facing retrieval (分库 C3).
"""
from __future__ import annotations

from dws_agent.kdl import retrieve, store
from dws_agent.kdl.ingest import Ingestor
from dws_agent.kdl.model import Taint

from kdl_helpers import make_paths, open_kdl


def _ingest_one(ing, cand):
    rep = ing.ingest_candidates([cand])
    assert rep.ingested, f"candidate was dropped: {rep.dropped}"
    return rep


SECRET_BODY = (
    "部署时用这个 token sk-ABCDEFGHIJ1234567890KLMNOPQR 调接口, "
    "数据库 password=SuperSecretPw99 , 联系人手机 13800138000 。"
)


def test_ingest_redacts_secrets_and_pii(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    cand = {
        "source_type": "ISSUE",
        "title": "部署接口的凭据",
        "body": SECRET_BODY,
        "provenance": [{"kind": "ISSUE_URL", "ref": "http://x/i/1", "quote": "见正文"}],
    }
    rep = _ingest_one(ing, cand)
    assert rep.redacted_count == 1
    ku = store.get_ku(conn, rep.ingested[0], key)
    assert ku.body_redacted is True
    # The high-entropy token and the phone number are redacted out of the body.
    assert "sk-ABCDEFGHIJ1234567890KLMNOPQR" not in ku.body
    assert "13800138000" not in ku.body
    assert "REDACTED" in ku.body
    # Secret present => taint upgraded to SENSITIVE (never washes down).
    assert ku.taint == Taint.SENSITIVE


def test_phone_only_content_is_internal_taint(tmp_path, monkeypatch):
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    cand = {
        "source_type": "QA",
        "title": "联系人",
        "body": "运维值班电话 13900139000 , 有问题打这个。",
        "provenance": [{"kind": "MSG_ID", "ref": "m-7", "quote": ""}],
    }
    rep = _ingest_one(ing, cand)
    ku = store.get_ku(conn, rep.ingested[0], key)
    assert "13900139000" not in ku.body
    # Phone is PII -> INTERNAL (not SENSITIVE, not washed to CLEAN).
    assert ku.taint == Taint.INTERNAL


def test_declared_taint_never_washes_down(tmp_path, monkeypatch):
    """A SENSITIVE declared taint is preserved even when text looks clean."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    cand = {
        "source_type": "QA",
        "title": "内部流程",
        "body": "走内部审批通道即可。",
        "declared_taint": "SENSITIVE",
        "provenance": [{"kind": "MSG_ID", "ref": "m-9", "quote": ""}],
    }
    rep = _ingest_one(ing, cand)
    ku = store.get_ku(conn, rep.ingested[0], key)
    assert ku.taint == Taint.SENSITIVE


def test_plaintext_body_is_never_persisted_in_the_db(tmp_path, monkeypatch):
    """Grep the raw DB bytes: a unique plaintext marker must not appear (§3.5)."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    # Low-entropy natural-language marker that redaction will NOT touch, so any
    # appearance on disk would prove the body was stored as plaintext.
    marker = "独特明文标记权限校验逻辑的具体说明文字"
    cand = {
        "source_type": "QA",
        "title": "权限校验",
        "body": f"答复正文 {marker} 详情见文档。",
        "provenance": [{"kind": "MSG_ID", "ref": "m-1",
                         "quote": f"问题里也含 {marker}"}],
    }
    rep = _ingest_one(ing, cand)
    # Decrypts correctly in-memory.
    ku = store.get_ku(conn, rep.ingested[0], key)
    assert marker in ku.body

    # Force WAL flush, then scan the on-disk bytes for the plaintext marker.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    raw = b""
    for suffix in ("", "-wal", "-shm"):
        p = paths.state_dir / ("state.db" + suffix)
        if p.exists():
            raw += p.read_bytes()
    assert marker.encode("utf-8") not in raw
    # The body column is a cipher BLOB, not text.
    row = conn.execute(
        "SELECT body_cipher FROM ku WHERE ku_id=?;", (rep.ingested[0],)
    ).fetchone()
    assert isinstance(row["body_cipher"], (bytes, bytearray))
    assert marker.encode("utf-8") not in bytes(row["body_cipher"])


def test_sensitive_ku_excluded_from_external_facing_retrieval(tmp_path, monkeypatch):
    """external_facing retrieval (分库 C3) must not surface non-CLEAN KUs."""
    paths = make_paths(tmp_path / "home", monkeypatch)
    conn, key = open_kdl(paths)
    ing = Ingestor(paths, conn, key)
    # Make it confirmed+fresh so the ONLY thing keeping it out is taint.
    cand = {
        "source_type": "QA",
        "title": "内部接口密钥轮换流程说明",
        "body": "内部密钥轮换流程: 步骤一二三, 注意保密。",
        "declared_taint": "SENSITIVE",
        "provenance": [{"kind": "DOC_ID", "ref": "doc-rotate", "quote": ""}],
    }
    ku_id = _ingest_one(ing, cand).ingested[0]
    store.set_authority(conn, ku_id, "REVIEWED", reason="r")
    store.set_authority(conn, ku_id, "AUTHORITATIVE", reason="我已确认")
    store.set_freshness(conn, ku_id, "FRESH")

    q = "内部密钥轮换流程"
    # operator-facing (default): retrievable for my preview.
    internal = retrieve.serve(conn, key, q, external_facing=False)
    assert retrieve._val(internal.decision) == "ANSWERABLE"
    # external_facing: SENSITIVE KU filtered out -> no hit -> abstain.
    external = retrieve.serve(conn, key, q, external_facing=True)
    assert retrieve._val(external.decision) == "ABSTAIN"
