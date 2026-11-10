from base64 import b64decode
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sqlite3
import re
from tempfile import TemporaryDirectory
import unittest
import shutil
import subprocess
from unittest.mock import patch

from werkzeug.datastructures import MultiDict
from PIL import Image

from server import HTML_BRIDGE, create_app, persistent_owner_token


OWNER = {"Authorization": "Bearer local-test-token"}
LAN = {"REMOTE_ADDR": "192.168.50.38"}
LAN_ORIGIN = "http://192.168.51.158:8765"
PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGOUdJv9n4GBgYGJAQoAHUYB/QuQV1EAAAAASUVORK5CYII="
PNG = b64decode(PNG_BASE64)


class ServerTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the HTML bridge")
    def test_html_bridge_selection_visibility_clips_nested_scroll_and_ignores_stale(self):
        harness = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(0, 'utf8').replace(/^\s*<script>/, '').replace(/<\/script>\s*$/, '');
const listeners = {}, windowListeners = {}, messages = [], frames = [];
let rect = {left:10,top:10,right:50,bottom:20,width:40,height:10};
class Element {
  constructor() { this.parentElement = null; this.clientWidth = 100; this.clientHeight = 50; }
  closest(){return null}
  getBoundingClientRect(){return {left:0,top:0,right:100,bottom:50,width:100,height:50}}
  getClientRects(){return [1]}
  scrollIntoView(){}
}
const container = new Element(), node = {data:'Alpha',length:5,parentElement:container,isConnected:true};
class Range {
  setStart(n,o){this.startContainer=n;this.startOffset=o}
  setEnd(n,o){this.endContainer=n;this.endOffset=o}
  getBoundingClientRect(){return rect}
  getClientRects(){return [rect]}
  cloneRange(){return this}
}
const range = new Range(); range.setStart(node,0); range.setEnd(node,5);
let current = range;
const selection = {get isCollapsed(){return !current},get rangeCount(){return current?1:0},
  getRangeAt:()=>current,removeAllRanges(){current=null},addRange(value){current=value}};
const document = {body:{},head:{append(){}},createElement:()=>({}),
  createTreeWalker:()=>{let read=false;return {nextNode(){if(read)return false;read=true;this.currentNode=node;return true}}},
  addEventListener:(name,fn)=>{(listeners[name]??=[]).push(fn)}};
const parent={postMessage:data=>messages.push(data)};
const context = {URL,Element,Range,document,innerWidth:200,innerHeight:100,
  parent,location:{href:'http://127.0.0.1:8765/x'},
  getSelection:()=>selection, getComputedStyle:()=>({overflowX:'auto',overflowY:'auto',visibility:'visible'}),
  NodeFilter:{SHOW_TEXT:4,FILTER_REJECT:2,FILTER_ACCEPT:1}, requestAnimationFrame:fn=>frames.push(fn),
  addEventListener:(name,fn)=>{(windowListeners[name]??=[]).push(fn)}};
vm.runInNewContext(source,context);
listeners.selectionchange[0]();
assert.strictEqual(messages.at(-1).type,'lanreview:selection');
assert.strictEqual(messages.at(-1).selectionId,1);
assert.strictEqual(messages.at(-1).rect.right,50);
// Text remains inside iframe but leaves its nested scrollable container.
rect = {left:10,top:60,right:50,bottom:70,width:40,height:10};
windowListeners.scroll[0](); frames.shift()();
assert.strictEqual(messages.at(-1).type,'lanreview:selection-visibility');
assert.strictEqual(messages.at(-1).visible,false);
assert.strictEqual(messages.at(-1).selectionId,1);
// A queued old scroll cannot cancel a subsequent new selection.
windowListeners.scroll[0]();
rect = {left:10,top:10,right:50,bottom:20,width:40,height:10};
listeners.selectionchange[0]();
assert.strictEqual(messages.at(-1).selectionId,2);
const count=messages.length; frames.shift()(); assert.strictEqual(messages.length,count);
windowListeners.scroll[0](); frames.shift()();
assert.strictEqual(messages.at(-1).visible,true);
assert.strictEqual(messages.at(-1).selectionId,2);
const send = (data,source=parent,origin='http://127.0.0.1:8765') => windowListeners.message[0]({source,origin,data});
const clear = id => ({type:'lanreview:clear-selection',selectionId:id});
send(clear(1)); assert.strictEqual(current,range); // stale ID
send(clear(2),{}); assert.strictEqual(current,range); // wrong source
send(clear(2),parent,'http://bad.example'); assert.strictEqual(current,range); // wrong origin
// A user has changed Selection but selectionchange hasn't fired yet.
const newer = new Range(); newer.setStart(node,1); newer.setEnd(node,5); current=newer;
send(clear(2)); assert.strictEqual(current,newer);
current=range;
listeners.pointerdown[0]({pointerId:3,clientX:1,clientY:1,isPrimary:true,pointerType:'mouse',button:0});
send(clear(2)); assert.strictEqual(current,range); // active gesture
listeners.pointercancel[0]();
send(clear(2)); assert.strictEqual(current,null);
current=range; listeners.selectionchange[0](); // same text may be selected again
assert.strictEqual(messages.at(-1).selectionId,3);
send(clear(2)); assert.strictEqual(current,range); // delayed clear from previous selection
send({type:'lanreview:locate',quote:'Alpha'}); // historical comment lookup is not a draft
const located=current; send(clear(3)); assert.strictEqual(current,located);
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], input=HTML_BRIDGE,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the HTML bridge")
    def test_html_bridge_escape_and_pointer_events(self):
        harness = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(0, 'utf8').replace(/^\s*<script>/, '').replace(/<\/script>\s*$/, '');
const listeners = {}, sent = [];
class Element { constructor(editable = false) { this.editable = editable; } closest() { return this.editable ? this : null; } }
let selection = {isCollapsed:true, rangeCount:0};
const document = {createElement:()=>({}), head:{append(){}}, addEventListener:(name, fn)=>{listeners[name]=fn;}};
const context = {URL, Element, document, location:{href:'http://127.0.0.1:8765/media/test'},
    parent:{postMessage:(message, target)=>sent.push([message.type,target])},
    getSelection:()=>selection, addEventListener:()=>{}};
vm.runInNewContext(source, context);
const content = new Element(), input = new Element(true);
listeners.keydown({key:'Escape', target:content, defaultPrevented:false, isComposing:false});
listeners.keydown({key:'Escape', target:input, defaultPrevented:false, isComposing:false});
listeners.keydown({key:'Escape', target:content, defaultPrevented:true, isComposing:false});
listeners.keydown({key:'Enter', target:content, defaultPrevented:false, isComposing:false});
const down = {pointerId:1, clientX:10, clientY:10, isPrimary:true, pointerType:'touch'};
listeners.pointerdown(down);
listeners.pointerup({...down,target:content});
listeners.pointerdown(down);
listeners.pointerup({...down,target:input});
listeners.pointerdown(down);
listeners.pointermove({...down, clientY:40});
listeners.pointerup({...down,target:content});
listeners.pointerdown(down);
listeners.pointercancel(down);
listeners.pointerup({...down,target:content});
selection = {isCollapsed:false, rangeCount:1};
listeners.pointerdown(down);
listeners.pointerup({...down,target:content});
assert.deepStrictEqual(sent, [['lanreview:escape','http://127.0.0.1:8765'],
    ['lanreview:selection-cleared','http://127.0.0.1:8765']]);
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], input=HTML_BRIDGE,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the HTML bridge")
    def test_html_bridge_locate_does_not_create_selection(self):
        harness = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(0, 'utf8').replace(/^\s*<script>/, '').replace(/<\/script>\s*$/, '');
const listeners = {}, messages = [], handlers = {};
class Element { closest(){return null} getClientRects(){return [1]} scrollIntoView(){} }
const element = new Element(), node = {data:'Alpha Beta', length:10, parentElement:element};
class Range { setStart(n,o){this.startContainer=n;this.startOffset=o} setEnd(n,o){this.endContainer=n;this.endOffset=o}
  getBoundingClientRect(){return {left:0,top:0,width:20,height:10}} }
let current = null;
const selection = {get isCollapsed(){return !current},get rangeCount(){return current?1:0},
  getRangeAt(){return current},removeAllRanges(){current=null},addRange(range){current=range}};
const document = {body:{}, createElement:()=>({}), head:{append(){}},
  createTreeWalker:()=>{let read=false;return {nextNode(){if(read)return false;read=true;this.currentNode=node;return true}}},
  addEventListener:(type,fn)=>{(listeners[type]??=[]).push(fn)}};
const parent = {postMessage:(data)=>messages.push(data)};
const context = {URL, Element, Range, document, parent, location:{href:'http://127.0.0.1:8765/x'},
  getSelection:()=>selection, getComputedStyle:()=>({visibility:'visible'}),
  NodeFilter:{SHOW_TEXT:4,FILTER_REJECT:2,FILTER_ACCEPT:1}, addEventListener:(type,fn)=>{handlers[type]=fn}};
vm.runInNewContext(source,context);
handlers.message({source:parent,origin:'http://127.0.0.1:8765',data:{type:'lanreview:locate',quote:'Alpha'}});
assert.strictEqual(messages.at(-1).type,'lanreview:locate-result');
listeners.selectionchange[0](); listeners.selectionchange[0]();
assert.strictEqual(messages.filter(x=>x.type==='lanreview:selection').length,0);
for(const fn of listeners.pointerdown)fn({pointerId:1,clientX:0,clientY:0,isPrimary:true,pointerType:'mouse',button:0});
current = new Range();current.setStart(node,6);current.setEnd(node,10);
listeners.selectionchange[0]();
assert.strictEqual(messages.at(-1).type,'lanreview:selection');
assert.strictEqual(messages.at(-1).quote,'Beta');
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], input=HTML_BRIDGE,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = self.new_app()
        self.client = self.app.test_client()

    def new_app(self):
        app = create_app(self.directory.name, "192.168.51.158", "192.168.50.0/23", owner_token="local-test-token")
        app.testing = True
        return app

    def upload_request(self, name, data, attachments):
        multipart = MultiDict([("file", (BytesIO(data), name))])
        for filename, image in attachments:
            multipart.add("attachments", (BytesIO(image), filename))
        return self.client.post("/api/reviews", data=multipart, headers=OWNER)

    def upload(self, name, data=b"# Test", attachments=()):
        result = self.upload_request(name, data, attachments)
        self.assertEqual(result.status_code, 201, result.json)
        self.assertRegex(result.json["id"], r"^[A-Za-z0-9_-]{32}$")
        return result.json["id"]

    def guest(self, url, **options):
        with self.app.test_client() as client:
            return client.get(url, base_url=LAN_ORIGIN, environ_overrides=LAN, buffered=True, **options)

    def comment(self, review_id, key=None, **fields):
        with self.app.test_client() as client:
            return client.post(
                f"/api/reviews/{review_id}/comments",
                json={"author": "测试者", "body": "保留这个细节", **fields},
                base_url=LAN_ORIGIN, environ_overrides=LAN,
                headers={"Idempotency-Key": key} if key is not None else {},
            )

    def test_shared_access_and_network_boundaries(self):
        review_id = self.upload("../../片段.md")
        response = self.client.get("/api/reviews", headers=OWNER)
        self.assertEqual(response.json["reviews"][0]["name"], "片段.md")
        self.assertEqual(response.json["reviews"][0]["url"], f"{LAN_ORIGIN}/review/{review_id}")
        self.assertEqual(response.json["reviews"][0]["share_scope"], "lan")
        self.assertEqual(self.client.get(f"/api/reviews/{review_id}").json["url"], f"{LAN_ORIGIN}/review/{review_id}")
        self.assertEqual(self.client.get(f"/api/reviews/{review_id}").json["share_scope"], "lan")
        self.assertEqual(self.client.get("/api/reviews").status_code, 200)
        self.assertEqual(self.client.post("/api/reviews").status_code, 400)
        self.assertEqual(self.guest("/").status_code, 200)
        self.assertEqual(self.guest("/api/reviews").status_code, 200)
        self.assertEqual(self.client.get("/api/reviews", base_url=LAN_ORIGIN, headers=OWNER).status_code, 200)
        response = self.client.post("/api/reviews", base_url=LAN_ORIGIN, environ_overrides=LAN,
            data={"file": (BytesIO(b"# LAN"), "lan.md")})
        self.assertEqual(response.status_code, 201)
        self.assertIn("lan.md", [item["name"] for item in self.guest("/api/reviews").json["reviews"]])
        self.assertEqual(self.client.post("/api/reviews", base_url=LAN_ORIGIN, environ_overrides=LAN,
            headers={"Origin": "http://untrusted.example"}, data={"file": (BytesIO(b"# X"), "x.md")}).status_code, 403)
        self.assertEqual(self.client.get("/api/reviews", base_url=LAN_ORIGIN,
            environ_overrides={"REMOTE_ADDR": "192.168.52.38"}).status_code, 403)
        self.assertEqual(self.client.post("/api/reviews", base_url=LAN_ORIGIN,
            environ_overrides={"REMOTE_ADDR": "192.168.52.38"}, data={"file": (BytesIO(b"# X"), "x.md")}).status_code, 403)
        self.assertEqual(self.client.get("/api/reviews", base_url="http://untrusted.example", headers=OWNER).status_code, 400)
        self.assertEqual(self.guest(f"/api/reviews/{review_id}").status_code, 200)
        self.assertEqual(self.guest("/media/../../../reviews.sqlite3").status_code, 404)
        self.assertEqual(self.guest("/api/reviews/" + "a" * 32).status_code, 404)
        response = self.client.get(
            f"/api/reviews/{review_id}", base_url=LAN_ORIGIN,
            environ_overrides={"REMOTE_ADDR": "192.168.52.38"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            f"/api/reviews/{review_id}/comments",
            base_url=LAN_ORIGIN, environ_overrides=LAN, headers={"Origin": "null"},
            json={"author": "X", "body": "Y"},
        )
        self.assertEqual(response.status_code, 403)

    def test_persistent_owner_token_and_local_access(self):
        path = Path(self.directory.name)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: persistent_owner_token(path), range(16)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual((path / "owner-token").stat().st_mode & 0o777, 0o600)
        first = create_app(path, "192.168.51.158", "192.168.50.0/23")
        second = create_app(path, "192.168.51.158", "192.168.50.0/23")
        headers = {"Authorization": "Bearer " + tokens[0]}
        with second.test_client() as client:
            self.assertEqual(client.get("/api/reviews", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/reviews", headers=headers, base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 200)
        (path / "owner-token").chmod(0o644)
        with self.assertRaises(ValueError):
            persistent_owner_token(path)

    def test_owner_token_rejects_symlink_and_corruption(self):
        path = Path(self.directory.name)
        target = path / "elsewhere"
        target.write_text("untouched")
        (path / "owner-token").symlink_to(target)
        with self.assertRaises(OSError):
            persistent_owner_token(path)
        self.assertEqual(target.read_text(), "untouched")
        (path / "owner-token").unlink()
        token_file = path / "owner-token"
        token_file.write_text("bad")
        token_file.chmod(0o600)
        with self.assertRaises(ValueError):
            persistent_owner_token(path)
    def test_loopback_share_scope(self):
        app = create_app(self.directory.name, port=8765, owner_token="local-test-token")
        with app.test_client() as client:
            result = client.post("/api/reviews", data={"file": (BytesIO(b"# local"), "local.md")}, headers=OWNER)
            self.assertEqual(result.status_code, 201)
            review_id = result.json["id"]
            self.assertEqual(result.json["share_scope"], "local")
            self.assertEqual(result.json["url"], f"/review/{review_id}")
            self.assertEqual(client.get(f"/api/reviews/{review_id}").json["share_scope"], "local")

    def test_upload_preserves_bytes_and_cleans_failed_input(self):
        source = "# 保留字节\r\n".encode("utf-8")
        review_id = self.upload("notes.md", source)
        stored = Path(self.directory.name, "files")
        self.assertEqual((stored / review_id).read_bytes(), source)
        for name, data, status in (("bad.md", b"\xff", 415), ("empty.md", b"", 400), ("bad.exe", b"x", 415)):
            response = self.client.post("/api/reviews", data={"file": (BytesIO(data), name)}, headers=OWNER)
            self.assertEqual(response.status_code, status)
        self.assertEqual(list(stored.iterdir()), [stored / review_id])

    def test_local_import_resolves_distinct_paths_and_reports_missing(self):
        import re
        root = Path(self.directory.name, "source")
        (root / "docs").mkdir(parents=True)
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "a" / "image.png").write_bytes(PNG)
        (root / "b" / "image.png").write_bytes(PNG + b"second")
        source = root / "docs" / "note.md"
        source.write_text("![A](../a/image.png) ![B](../b/image.png) ![C](missing.png) ![R](https://example.com/a.png)", encoding="utf-8")
        response = self.client.post("/api/reviews/import", json={"path": str(source)}, headers=OWNER)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(len(response.json["warnings"]), 2)
        review_id = response.json["id"]
        html = self.guest(f"/api/reviews/{review_id}/rendered").json["html"]
        urls = re.findall(r'src="([^"]+)', html)
        self.assertEqual(len(urls), 2, html)
        self.assertNotEqual(*urls)
        self.assertEqual(self.guest(urls[0]).data, PNG)
        self.assertEqual(self.guest(urls[1]).data, PNG + b"second")
        self.assertIn("图片不可用", html)
        self.assertEqual(self.client.post("/api/reviews/import", json={"path": "relative.md"}).status_code, 400)
        self.assertEqual(self.client.post("/api/reviews/import", json={"path": "relative.md"}, headers=OWNER).status_code, 400)
        self.assertEqual(self.client.post("/api/reviews/import", json={"path": str(source)}, headers=OWNER,
            base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 403)
        self.app = self.new_app()
        self.assertEqual(self.guest(urls[0]).data, PNG)

    def test_local_import_rejects_symlinks_and_special_files(self):
        root = Path(self.directory.name)
        image = root / "actual.png"
        image.write_bytes(PNG)
        (root / "link.png").symlink_to(image)
        source = root / "note.md"
        source.write_text("![link](link.png)", encoding="utf-8")
        result = self.client.post("/api/reviews/import", json={"path": str(source)}, headers=OWNER)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(len(result.json["warnings"]), 1)
        self.assertEqual(self.client.post("/api/reviews/import", json={"path": str(root / 'link.png')}, headers=OWNER).status_code, 400)

    def test_local_picker_restricted_to_host_and_handles_cancel(self):
        from subprocess import CompletedProcess
        with patch.dict("os.environ", {"DISPLAY": ":0"}), patch("server.subprocess.run") as run:
            self.assertEqual(self.client.post("/api/reviews/pick-local", base_url=LAN_ORIGIN,
                environ_overrides=LAN, headers=OWNER).status_code, 403)
            run.assert_not_called()
            run.return_value = CompletedProcess([], 1, b"")
            result = self.client.post("/api/reviews/pick-local")
            self.assertEqual(result.json, {"cancelled": True})
            source = Path(self.directory.name, "chosen.md")
            source.write_text("# 选择", encoding="utf-8")
            run.return_value = CompletedProcess([], 0, (str(source) + "\n").encode())
            result = self.client.post("/api/reviews/pick-local", headers=OWNER)
            self.assertEqual(result.status_code, 201, result.json)
            self.assertEqual(result.json["name"], "chosen.md")
            self.assertEqual(result.json["warnings"], [])

    def test_threads_remain_after_restart_and_concurrent_writes(self):
        review_id = self.upload("notes.md")
        other_id = self.upload("another.md")
        anchor = {"type": "text", "quote": "Test", "prefix": "", "suffix": ""}
        response = self.comment(review_id, anchor=anchor)
        self.assertEqual(response.status_code, 201)
        parent = response.json
        reply = self.comment(review_id, parent_id=parent["id"])
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json["parent_id"], parent["id"])
        self.assertEqual(self.comment(other_id, parent_id=parent["id"]).status_code, 400)
        self.assertEqual(self.comment(review_id, parent_id=reply.json["id"]).status_code, 400)
        self.assertEqual(self.comment(review_id, parent_id=True).status_code, 400)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda i: self.comment(review_id, author=f"评审者 {i}"), range(16)))
        self.assertTrue(all(result.status_code == 201 for result in results))
        ids = {result.json["id"] for result in results}
        self.assertEqual(len(ids), 16)
        self.app = self.new_app()
        comments = self.guest(f"/api/reviews/{review_id}").json["comments"]
        self.assertEqual(len(comments), 18)
        self.assertEqual(comments[0]["anchor"], anchor)
        self.assertEqual(comments[1]["parent_id"], parent["id"])
        self.assertEqual({comment["id"] for comment in comments[2:]}, ids)

    def test_idempotent_comment_retries(self):
        review_id = self.upload("notes.md")
        key = "0c56202903ee48aebba786f8ea38a58f"
        parent = self.comment(review_id).json
        with ThreadPoolExecutor(max_workers=8) as executor:
            attempts = list(executor.map(lambda _: self.comment(review_id, key, parent_id=parent["id"]), range(8)))
        for attempt in attempts:
            self.assertEqual(attempt.status_code, 201, attempt.json)
            self.assertEqual(attempt.json, attempts[0].json)
        self.assertEqual(self.comment(review_id, key, body="另一条评论").status_code, 409)
        self.assertEqual(self.comment(review_id, "short").status_code, 400)
        self.app = self.new_app()
        replay = self.comment(review_id, key, parent_id=parent["id"])
        self.assertEqual(replay.json, attempts[0].json)
        self.assertEqual(self.guest(f"/api/reviews/{review_id}").json["comments"],
                         [{**parent, "can_delete": False}, {**replay.json, "can_delete": False}])

    def test_anonymous_identity_and_retry(self):
        review_id = self.upload("anonymous.md")
        path = f"/api/reviews/{review_id}"
        one = self.app.test_client()
        two = self.app.test_client()
        for client in (one, two):
            response = client.get(path, base_url=LAN_ORIGIN, environ_overrides=LAN)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        def post(client, payload, key=None):
            return client.post(path + "/comments", json=payload, base_url=LAN_ORIGIN,
                               environ_overrides=LAN,
                               headers={"Idempotency-Key": key} if key else {})
        first = post(one, {"author": "", "body": "第一条"}).json
        reply = post(one, {"body": "回复", "parent_id": first["id"]}).json
        other = post(two, {"author": " ", "body": "另一位"}).json
        self.assertRegex(first["author"], r"^匿名-[0-9a-f]{10}$")
        self.assertEqual(first["author"], reply["author"])
        self.assertNotEqual(first["author"], other["author"])
        self.assertNotIn("172.16", first["author"])
        self.app = self.new_app()
        self.assertEqual(post(one, {"body": "重启后"}).json["author"], first["author"])
        key = "3c56202903ee48aebba786f8ea38a58f"
        # No cookies and a lost first response: retry still returns the same comment.
        result = post(self.app.test_client(use_cookies=False), {"body": "重试"}, key)
        retry = post(self.app.test_client(use_cookies=False), {"body": "重试"}, key)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json, retry.json)
        # Parallel tabs can overwrite a newly issued cookie after a lost response.
        race_key = "4c56202903ee48aebba786f8ea38a58f"
        created = post(one, {"body": "并行标签"}, race_key)
        replayed = post(two, {"body": "并行标签"}, race_key)
        self.assertNotEqual(first["author"], other["author"])
        self.assertEqual({**created.json, "can_delete": False}, replayed.json)
        self.assertEqual(post(two, {"body": "并行标签改动"}, race_key).status_code, 409)
        self.assertEqual(post(two, {"author": "名字", "body": "并行标签"}, race_key).status_code, 409)
        self.assertEqual(post(one, {"author": "名字" * 33, "body": "超长"}).status_code, 400)

    def test_delete_own_comments_preserves_thread_and_legacy(self):
        review_id = self.upload("delete.md")
        path = f"/api/reviews/{review_id}"
        one, two = self.app.test_client(), self.app.test_client()
        for client in (one, two):
            client.get(path, base_url=LAN_ORIGIN, environ_overrides=LAN)
        key = "5c56202903ee48aebba786f8ea38a58f"
        payload = {"author": "相同名称", "body": "原始评论", "anchor": {"type": "text", "quote": "Test"}}
        parent = one.post(path + "/comments", json=payload, headers={"Idempotency-Key": key},
                          base_url=LAN_ORIGIN, environ_overrides=LAN).json
        reply = two.post(path + "/comments", json={"author": "相同名称", "body": "保留回复", "parent_id": parent["id"]},
                         base_url=LAN_ORIGIN, environ_overrides=LAN).json
        delete_path = path + f"/comments/{parent['id']}/delete"
        self.assertEqual(two.post(delete_path, base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 403)
        self.assertEqual(one.post(delete_path, base_url=LAN_ORIGIN, environ_overrides=LAN,
                                  headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(one.post(delete_path, base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 200)
        self.assertEqual(one.post(delete_path, base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 200)
        self.assertEqual(two.post(path + "/comments", json={"body": "已删除后的回复", "parent_id": parent["id"]},
                                  base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 400)
        self.assertEqual(one.post(path + "/comments", json=payload, headers={"Idempotency-Key": key},
                                  base_url=LAN_ORIGIN, environ_overrides=LAN).status_code, 409)
        self.app = self.new_app()
        comments = one.get(path, base_url=LAN_ORIGIN, environ_overrides=LAN).json["comments"]
        self.assertTrue(comments[0]["deleted"])
        self.assertEqual(comments[0]["author"], "评论已删除")
        self.assertEqual(comments[0]["body"], "")
        self.assertEqual(comments[0]["anchor"], parent["anchor"])
        self.assertFalse(comments[0]["can_delete"])
        self.assertEqual(comments[1]["id"], reply["id"])
        self.assertEqual(comments[1]["body"], "保留回复")
        self.assertFalse(comments[1]["can_delete"])
        other_view = two.get(path, base_url=LAN_ORIGIN, environ_overrides=LAN).json["comments"]
        self.assertTrue(other_view[1]["can_delete"])
        with sqlite3.connect(Path(self.directory.name) / "reviews.sqlite3") as db:
            db.execute("UPDATE comments SET owner_hash = NULL WHERE id = ?", (reply["id"],))
        self.assertEqual(two.post(path + f"/comments/{reply['id']}/delete", base_url=LAN_ORIGIN,
                                  environ_overrides=LAN).status_code, 403)

    def test_anchor_validation_and_media_ranges(self):
        video_id = self.upload("video.mp4", bytes(range(100)))
        response = self.guest(f"/media/{video_id}", headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["Content-Range"], "bytes 10-19/100")
        self.assertEqual(response.data, bytes(range(10, 20)))
        self.assertEqual(self.guest(f"/media/{video_id}", headers={"Range": "bytes=200-210"}).status_code, 416)
        self.assertEqual(self.comment(video_id, anchor={"type": "video", "time": 2.5}).status_code, 201)
        self.assertEqual(self.comment(video_id, anchor={"type": "video", "time": True}).status_code, 400)
        self.assertEqual(self.comment(video_id, anchor={"type": "video", "time": -1}).status_code, 400)
        self.assertEqual(self.comment(video_id, anchor={"type": "image", "x": .5, "y": .5}).status_code, 400)
        image_id = self.upload("pic.png", PNG)
        self.assertEqual(self.comment(image_id, anchor={"type": "image", "x": .5, "y": 1}).status_code, 201)
        self.assertEqual(self.comment(image_id, anchor={"type": "image", "x": .5, "y": 1.01}).status_code, 400)

    def test_visual_regions_persist_and_reject_invalid_geometry(self):
        video_id = self.upload("clip.mp4", bytes(range(100)))
        image_id = self.upload("picture.png", PNG)
        video_region = {"type": "video", "time": 6.25,
                        "region": {"type": "rect", "x": .1, "y": .2, "w": .3, "h": .4}}
        point_region = {"type": "image", "region": {"type": "point", "x": 1, "y": 0}}
        image_region = {"type": "image", "region": {"type": "rect", "x": .2, "y": .4, "w": .2, "h": .3}}
        polygon_region = {"type": "video", "time": 6.25, "region": {"type": "polygon",
                          "points": [[.1, .2], [.4, .2], [.2, .5]]}}
        for review_id, anchor in ((video_id, video_region), (image_id, point_region), (image_id, image_region),
                                  (video_id, polygon_region)):
            response = self.comment(review_id, anchor=anchor)
            self.assertEqual(response.status_code, 201, response.json)
            self.assertEqual(response.json["anchor"], anchor)
            stored = self.guest(f"/api/reviews/{review_id}").json["comments"]
            self.assertEqual(stored[-1]["anchor"], anchor)

        # A visual region belongs to its media type and must be a finite,
        # non-empty rectangle fully inside the normalized content area.
        bad_regions = [
            None, {"type": "polygon", "x": 0, "y": 0},
            {"type": "point", "x": True, "y": 0},
            {"type": "point", "x": float("nan"), "y": 0},
            {"type": "rect", "x": .9, "y": .1, "w": .2, "h": .1},
            {"type": "rect", "x": .1, "y": .1, "w": 0, "h": .1},
            {"type": "polygon", "points": [[0, 0], [1, 1]]},
            {"type": "polygon", "points": [[0, 0], [1, 1], [.5, .5]]},
            {"type": "polygon", "points": [[0, 0], [1, True], [.5, .5]]},
            {"type": "polygon", "points": [[0, 0], [1, 0], [.5, 1.1]]},
            {"type": "polygon", "points": [[0, 0]] * 257},
        ]
        for region in bad_regions:
            with self.subTest(region=region):
                self.assertEqual(self.comment(video_id, anchor={"type": "video", "time": 1, "region": region}).status_code, 400)
        self.assertEqual(self.comment(image_id, anchor=video_region).status_code, 400)
        self.assertEqual(self.comment(video_id, anchor=image_region).status_code, 400)

    def test_video_time_ranges_preserve_anchors_and_idempotent_concurrency(self):
        video_id = self.upload("clip.mp4", bytes(range(100)))
        legacy = {"type": "video", "time": 2}
        first = self.comment(video_id, anchor=legacy)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json["anchor"], legacy)
        anchor = {"type": "video", "time": 2.25, "end_time": 5.5,
                  "region": {"type": "rect", "x": .1, "y": .2, "w": .3, "h": .4}}
        key = "concurrent-video-range-key-12345"
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: self.comment(video_id, key=key, anchor=anchor), range(6)))
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertEqual(len({response.json["id"] for response in responses}), 1)
        self.assertTrue(all(response.json["anchor"] == anchor for response in responses))
        stored = self.guest(f"/api/reviews/{video_id}").json["comments"]
        self.assertEqual([item["anchor"] for item in stored], [legacy, anchor])
        self.assertEqual(self.comment(video_id, key=key, anchor={**anchor, "end_time": 6}).status_code, 409)
        for invalid in (None, True, "3", -1, 2.25, 2, 10**9 + 1, float("nan"), float("inf")):
            with self.subTest(end_time=invalid):
                self.assertEqual(self.comment(video_id, anchor={**anchor, "end_time": invalid}).status_code, 400)
        # The model represents a temporal range and an optional region on the
        # start frame only; no intermediate-frame geometry is inferred.
        ranged = self.comment(video_id, anchor={"type": "video", "time": 0, "end_time": 1})
        self.assertEqual(ranged.status_code, 201)
        self.assertNotIn("region", ranged.json["anchor"])

    def test_remote_markdown_bundle_matches_only_actual_full_references(self):
        source = (b"# Picture\n\n![a](../images/a/image.png)\n\n"
                  b"![b](../images/b/image.png)\n\n![missing](missing.png)\n\n"
                  b"![remote](https://example.invalid/track.png)\n")
        response = self.client.post("/api/markdown/images", data={"file": (BytesIO(source), "note.md")},
                                    base_url=LAN_ORIGIN, environ_overrides=LAN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["reference"] for item in response.json["references"]],
                         ["../images/a/image.png", "../images/b/image.png", "missing.png", None])
        data = MultiDict([("file", (BytesIO(source), "note.md")),
                          ("references", "../images/a/image.png"),
                          ("references", "../images/b/image.png"),
                          ("attachments", (BytesIO(PNG), "image.png")),
                          ("attachments", (BytesIO(PNG), "image.png"))])
        result = self.client.post("/api/reviews/bundle", data=data,
                                  base_url=LAN_ORIGIN, environ_overrides=LAN)
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(len(result.json["warnings"]), 2)
        detail = self.guest(f"/api/reviews/{result.json['id']}").json
        html = self.guest(detail["rendered_url"]).json["html"]
        urls = re.findall(r'<img[^>]+src="([^"]+)"', html)
        self.assertEqual(len(urls), 2)
        self.assertNotEqual(urls[0], urls[1])
        self.assertTrue(all(self.guest(url).data == PNG for url in urls))

    def test_remote_markdown_bundle_rejects_forgery_and_cleans_partial_files(self):
        source = b"# P\n\n![a](a.png)\n\n![b](b.png)"
        baseline = {item.name for item in (Path(self.directory.name) / "files").iterdir()}
        before = self.guest("/api/reviews").json["reviews"]
        cases = [
            [("references", "../other.png"), ("attachments", (BytesIO(PNG), "a.png"))],
            [("references", "a.png"), ("references", "a.png"),
             ("attachments", (BytesIO(PNG), "a.png")), ("attachments", (BytesIO(PNG), "a.png"))],
            [("references", "a.png")],
            [("references", "a.png"), ("references", "b.png"),
             ("attachments", (BytesIO(PNG), "a.png")), ("attachments", (BytesIO(b"not an image"), "b.png"))],
        ]
        for pairs in cases:
            data = MultiDict([("file", (BytesIO(source), "note.md")), *pairs])
            response = self.client.post("/api/reviews/bundle", data=data,
                                        base_url=LAN_ORIGIN, environ_overrides=LAN)
            self.assertIn(response.status_code, (400, 415), response.json)
            self.assertEqual(self.guest("/api/reviews").json["reviews"], before)
            self.assertEqual({item.name for item in (Path(self.directory.name) / "files").iterdir()}, baseline)

    def test_smart_selection_endpoint_limits_and_result_validation(self):
        review_id = self.upload("clip.mp4", bytes(range(100)))
        text_id = self.upload("note.md")
        def submit(review=review_id, data=None, **kwargs):
            return self.client.post(f"/api/reviews/{review}/segment", base_url=LAN_ORIGIN,
                environ_overrides=LAN, data=data or {"image": (BytesIO(PNG), "frame.png"), "x": ".5", "y": ".25"}, **kwargs)
        candidate = {"points": [[0, 0], [1, 0], [0, 1]], "score": .8}
        with patch("smart_select.segment_image", return_value={"candidates": [candidate]}) as select:
            response = submit()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {"candidates": [candidate]})
            select.assert_called_once_with(PNG, .5, .25)
        self.assertEqual(submit(review=text_id).status_code, 400)
        self.assertEqual(submit(data={"image": (BytesIO(PNG), "frame.png"), "x": "nan", "y": ".5"}).status_code, 400)
        self.assertEqual(submit(data={"image": (BytesIO(b"not an image"), "frame.png"), "x": ".5", "y": ".5"}).status_code, 415)
        self.assertEqual(submit(data={"image": (BytesIO(PNG), "frame.png"), "x": ".5", "y": ".5"},
                                headers={"Origin": "http://untrusted.example"}).status_code, 403)
        with patch("smart_select.segment_image", return_value={"candidates": [{"points": [[0, 0]], "score": 1}]}):
            self.assertEqual(submit().status_code, 503)
        from smart_select import SmartSelectError
        with patch("smart_select.segment_image", side_effect=SmartSelectError("本机智能选物模型未安装，请改用手动选区。")):
            response = submit()
            self.assertEqual(response.status_code, 503)
            self.assertIn("手动选区", response.json["error"])

    def test_video_comment_thumbnail_validation_lifecycle_and_retry(self):
        video_id = self.upload("clip.mp4", bytes(range(100)))
        other_video_id = self.upload("other.mp4", bytes(range(100)))
        image_id = self.upload("picture.png", PNG)
        def jpeg(width=32, height=18):
            output = BytesIO()
            Image.new("RGB", (width, height), "white").save(output, "JPEG")
            return output.getvalue()
        image_bytes = jpeg()
        thumbnail = "data:image/jpeg;base64," + b64encode(image_bytes).decode()
        key = "thumbnail-test-key-1234567890"
        url = f"/api/reviews/{video_id}/comments"
        payload = {"body": "这帧的位置", "anchor": {"type": "video", "time": 1.5}, "thumbnail": thumbnail}
        self.client.get(f"/api/reviews/{video_id}")
        result = self.client.post(url, json=payload, headers={"Idempotency-Key": key})
        self.assertEqual(result.status_code, 201, result.json)
        thumbnail_url = result.json["thumbnail_url"]
        self.assertEqual(self.guest(thumbnail_url).data, image_bytes)
        self.assertEqual(self.guest(thumbnail_url).mimetype, "image/jpeg")
        self.assertEqual(self.guest(f"/api/reviews/{video_id}").json["comments"][-1]["thumbnail_url"], thumbnail_url)
        with self.new_app().test_client() as restarted:
            self.assertEqual(restarted.get(thumbnail_url).data, image_bytes)
        self.assertEqual(self.client.post(url, json=payload, headers={"Idempotency-Key": key}).json["id"], result.json["id"])
        self.assertEqual(self.client.post(url, json={**payload, "thumbnail": None}, headers={"Idempotency-Key": key}).status_code, 409)
        self.assertEqual(self.guest(thumbnail_url.replace(video_id, other_video_id)).status_code, 404)
        self.assertEqual(self.comment(image_id, anchor={"type": "image", "x": .5, "y": .5}, thumbnail=thumbnail).status_code, 400)
        invalid = ["data:image/png;base64," + b64encode(PNG).decode(),
                   "data:image/jpeg;base64," + b64encode(jpeg(641, 1)).decode(),
                   "data:image/jpeg;base64," + b64encode(b"\xff\xd8\xffgarbage").decode(),
                   "data:image/jpeg;base64," + b64encode(b"x" * (128 * 1024 + 1)).decode()]
        for bad in invalid:
            with self.subTest(thumbnail=bad[:50]):
                self.assertEqual(self.client.post(url, json={**payload, "thumbnail": bad}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/reviews/{video_id}/comments/{result.json['id']}/delete").status_code, 200)
        self.assertEqual(self.guest(thumbnail_url).status_code, 404)
        self.assertIsNone(self.guest(f"/api/reviews/{video_id}").json["comments"][-1]["thumbnail_url"])

    def test_preview_safety_and_markdown_fidelity(self):
        source = (
            b"---\ntitle: Memo\ntags:\n- local\n---\n\n# Notes\n\n- [ ] Pending\n- [x] Done\n\n"
            b"[external](https://example.invalid) [bad](javascript:alert(1)) [bad](data:text/html,hi)\n\n"
            b"![track](https://example.invalid/pixel)\n\n"
            b"<img src=x onerror=alert(1)>\n\n```html\n<a>\n```"
        )
        review_id = self.upload("sample.md", source)
        detail = self.guest(f"/api/reviews/{review_id}").json
        self.assertEqual(self.guest(detail["content_url"]).data, source)
        rendered = self.guest(detail["rendered_url"]).json
        result = rendered["html"]
        self.assertEqual(rendered["metadata"], "title: Memo\ntags:\n- local")
        self.assertNotIn("title: Memo", result)
        self.assertIn('<input disabled type="checkbox"> Pending', result)
        self.assertIn('<input checked disabled type="checkbox"> Done', result)
        self.assertIn('<h1 id="notes">Notes</h1>', result)
        self.assertIn("&lt;a&gt;", result)
        self.assertIn("&lt;img", result)
        self.assertIn('href="https://example.invalid" rel="noopener noreferrer" target="_blank"', result)
        self.assertNotIn("https://example.invalid/pixel", result)
        self.assertIn("图片不可用：track", result)
        self.assertNotIn('href="javascript:', result)
        self.assertNotIn('href="data:', result)
        review_id = self.upload("interactive.html", b"<button>Use me</button><script>var active = true;</script>")
        media_url = f"/media/{review_id}"
        response = self.guest(media_url, headers={"Sec-Fetch-Dest": "iframe"})
        self.assertIn(b"var active = true;", response.data)
        self.assertIn(b"lanreview:selection", response.data)
        self.assertIn("sandbox allow-scripts;", response.headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        download = self.guest(media_url, headers={"Sec-Fetch-Dest": "document"})
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertNotIn(b"lanreview:selection", download.data)

    def test_markdown_attachment_resolution_and_access(self):
        source = (
            f"![可见](../raw/figures/%E5%9B%BE.jpg)\n\n"
            f"![内嵌](data:image/png;base64,{PNG_BASE64})\n\n"
            "![缺图](missing.png)\n\n![一](one/repeat.png)\n\n![二](two/repeat.png)\n\n"
            "![远程](https://example.invalid/%E5%9B%BE.jpg)"
        ).encode()
        review_id = self.upload("notes.md", source, (("图.jpg", PNG), ("repeat.png", PNG)))
        other_id = self.upload("other.md")
        self.app = self.new_app()
        rendered = self.guest(f"/api/reviews/{review_id}/rendered").json["html"]
        self.assertIn(f'src="data:image/png;base64,{PNG_BASE64}"', rendered)
        self.assertIn("图片不可用：缺图", rendered)
        self.assertIn("图片不可用：一", rendered)
        self.assertIn("图片不可用：二", rendered)
        self.assertIn("图片不可用：远程", rendered)
        self.assertRegex(rendered, rf'/media/{review_id}/attachments/[A-Za-z0-9_-]{{32}}')
        path = rendered.split(f'src="/media/{review_id}/attachments/', 1)[1].split('"', 1)[0]
        response = self.guest(f"/media/{review_id}/attachments/{path}")
        self.assertEqual(response.data, PNG)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(self.guest(f"/media/{other_id}/attachments/{path}").status_code, 404)
        self.assertEqual(self.guest(f"/media/{review_id}/attachments/..%2Freviews.sqlite3").status_code, 404)

    def test_invalid_attachment_upload_is_atomic(self):
        for name, attachments, status in (
            ("notes.md", (("../foo.png", PNG),), 400),
            ("notes.md", (("foo.png", PNG), ("foo.png", PNG)), 400),
            ("notes.md", (("fake.png", b"<script>"),), 415),
            ("notes.md", (("foo.svg", b"<svg>"),), 415),
            ("sample.html", (("foo.png", PNG),), 400),
        ):
            with self.subTest(name=name, attachments=attachments, status=status):
                response = self.upload_request(name, b"# Test", attachments)
                self.assertEqual(response.status_code, status, response.json)
        with patch("server.MAX_ATTACHMENT_BYTES", len(PNG)):
            response = self.upload_request("notes.md", b"# Test", (("one.png", PNG), ("two.png", PNG)))
            self.assertEqual(response.status_code, 413, response.json)
        self.assertEqual(self.client.get("/api/reviews", headers=OWNER).json["reviews"], [])
        self.assertEqual(list(Path(self.directory.name, "files").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
