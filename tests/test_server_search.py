"""Full-text search: Chinese, English and mixed queries, and what it must not find.

The index is a cache. Every test here is really asking the same question — does
the search result agree with what the base tables and the permission model say
right now — because that is the only place a stale index can hurt anyone.
"""

from __future__ import annotations

from server_support import banner, expect_error, make_admin, make_services, make_user

services = make_services()
admin = make_admin(services)
tree = services.tree
messages = services.messages
search = services.search


def hits(actor, query, **kwargs):
    return [(r["kind"], r["title"], r["id"]) for r in search.search(actor, query, **kwargs)["results"]]


def titles(actor, query, **kwargs):
    return {r["title"] for r in search.search(actor, query, **kwargs)["results"]}


banner("a workspace to search")
root = tree.create_project(admin, parent_id=None, name="重構計畫")
tree.update_project(
    admin,
    root["id"],
    revision=tree.get_project(admin, root["id"])["revision"],
    fields={"prompt": "所有回答都用繁體中文，程式碼註解用英文。"},
)
storage = tree.create_project(admin, parent_id=root["id"], name="儲存層")
ui = tree.create_project(admin, parent_id=root["id"], name="桌面介面")

sqlite_talk = tree.create_conversation(admin, project_id=storage["id"], name="SQLite 設計討論")
ui_talk = tree.create_conversation(admin, project_id=ui["id"], name="Tk 介面調整")

messages.append(
    admin,
    sqlite_talk["id"],
    role="user",
    content="把 workspace.json 換成 SQLite，附件要用 chunked upload 放進資料庫。",
)
messages.append(
    admin,
    sqlite_talk["id"],
    role="agent",
    content="建議用單一 writer queue 序列化所有寫入，讀取連線各自獨立。",
)
messages.append(
    admin, ui_talk["id"], role="user", content="Résumé 這種帶重音的字也要能搜尋到。"
)
noisy = messages.append(
    admin, ui_talk["id"], role="tool", content="tool output: SQLite VACUUM finished in 0.2s"
)

banner("Chinese searches match inside a run of characters, not just at its start")
assert titles(admin, "附件", kinds=("message",)) == {"SQLite 設計討論"}
assert titles(admin, "序列化", kinds=("message",)) == {"SQLite 設計討論"}
assert titles(admin, "儲存", kinds=("project",)) == {"重構計畫 / 儲存層"}
print("『附件』、『序列化』、『儲存』 all matched mid-string")

banner("English, mixed and accented queries")
assert titles(admin, "workspace.json", kinds=("message",)) == {"SQLite 設計討論"}
assert titles(admin, "SQLite 附件", kinds=("message",)) == {"SQLite 設計討論"}
assert titles(admin, "resume", kinds=("message",)) == {"Tk 介面調整"}
assert titles(admin, "SQLite", kinds=("conversation",)) == {"SQLite 設計討論"}
print("English, mixed and diacritic-folded queries all work")

banner("project prompts are searchable")
found = search.search(admin, "繁體中文", kinds=("project",))["results"]
assert [r["id"] for r in found] == [root["id"]], found
print("matched a project by its prompt:", found[0]["summary"][:24])

banner("tool output is deliberately not indexed")
assert titles(admin, "VACUUM") == set()
assert messages.list_messages(admin, ui_talk["id"])["messages"][-1]["id"] == noisy["id"]
print("the tool message is stored and readable, just not in the index")

banner("search operators typed into the box are searched for, not executed")
for hostile in ('AND', 'NEAR("a" "b")', '"', "*", "content: OR"):
    search.search(admin, hostile)  # must not raise
print("FTS5 syntax in the query string is treated as literal text")

banner("results are filtered by permission before FTS ever runs")
alice = make_user(services, admin, "alice")
assert search.search(alice, "SQLite")["results"] == []
tree.grant(admin, storage["id"], user_id=alice.id, permission="viewer")
assert titles(alice, "SQLite") == {"SQLite 設計討論"}
# The sibling project she has no grant on stays invisible.
assert titles(alice, "介面") == set()
print("alice sees only what she was granted")

banner("moving a conversation reindexes it and everything under it")
tree.move_conversation(
    admin,
    sqlite_talk["id"],
    revision=tree.get_conversation(admin, sqlite_talk["id"])["revision"],
    project_id=ui["id"],
)
assert titles(alice, "SQLite") == set(), "alice must lose the moved conversation"
assert titles(admin, "序列化", kinds=("message",)) == {"SQLite 設計討論"}
moved = search.search(admin, "序列化", kinds=("message",))["results"][0]
assert moved["project_id"] == ui["id"], moved
print("the moved conversation's messages now filter under its new project")

banner("soft-deleted content disappears from search and comes back on restore")
tree.delete_conversation(admin, sqlite_talk["id"], revision=tree.get_conversation(admin, sqlite_talk["id"])["revision"])
assert titles(admin, "序列化") == set()
assert titles(admin, "SQLite", kinds=("conversation",)) == set()
tree.restore_conversation(admin, sqlite_talk["id"])
assert titles(admin, "序列化", kinds=("message",)) == {"SQLite 設計討論"}
print("delete removed it from the index; restore put it back")

banner("deleting a project takes its whole subtree out of the index")
tree.delete_project(admin, root["id"], revision=tree.get_project(admin, root["id"])["revision"])
assert search.search(admin, "SQLite")["results"] == []
assert search.search(admin, "重構")["results"] == []
tree.restore_project(admin, root["id"])
assert titles(admin, "重構", kinds=("project",)) == {"重構計畫"}
print("subtree delete and restore keep the index honest")

banner("editing a message updates what search finds")
target = messages.append(admin, ui_talk["id"], role="agent", content="第一版的答案")
assert titles(admin, "第一版", kinds=("message",)) == {"Tk 介面調整"}
messages.complete(admin, target["id"], content="修訂後的答案")
assert titles(admin, "第一版", kinds=("message",)) == set()
assert titles(admin, "修訂後", kinds=("message",)) == {"Tk 介面調整"}
print("the index followed the edit")

banner("an empty or punctuation-only query returns nothing rather than everything")
assert search.search(admin, "   ")["results"] == []
assert search.search(admin, "!!!")["results"] == []
expect_error(400, search.search, admin, "SQLite", kinds=("nonsense",))

services.close()
print("\ntest_server_search OK")
