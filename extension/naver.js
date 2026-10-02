// 네이버 블로그 글쓰기 창(스마트에디터 ONE)에 BlogScope 원고를 채운다. 발행 버튼은 사용자가 직접 누른다.
// 네이버 화면 구조가 바뀌면 아래 SEL 만 고치면 된다.
(function () {
  if (window.__blogscopeLoaded) return;
  window.__blogscopeLoaded = true;

  const MAX_AGE = 30 * 60 * 1000; // 30분 지난 원고는 무시
  const SEL = {
    editor: [".se-content", "#SE-root", ".se-wrap"],
    title: [".se-documentTitle .se-text-paragraph", ".se-section-documentTitle .se-text-paragraph",
            ".se-title-text .se-text-paragraph", ".se-title-text"],
    titleBox: ".se-documentTitle, .se-section-documentTitle, .se-title-text",
    body: [".se-component.se-text .se-text-paragraph", ".se-section-text .se-text-paragraph",
           ".se-component-content .se-text-paragraph"],
    popup: ".se-popup-alert, .se-popup-container, .se-popup",
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const first = (list) => { for (const s of list) { const e = document.querySelector(s); if (e) return e; } return null; };
  async function waitFor(fn, timeout) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) { const v = fn(); if (v) return v; await sleep(500); }
    return null;
  }
  const titleEl = () => first(SEL.title);
  function bodyEl() {
    for (const s of SEL.body) {
      for (const e of document.querySelectorAll(s)) if (!e.closest(SEL.titleBox)) return e;
    }
    return null;
  }
  const norm = (s) => String(s || "").replace(/\s+/g, "");

  function focusEnd(el) {
    el.scrollIntoView({ block: "center" });
    for (const t of ["mousedown", "mouseup", "click"]) {
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    }
    if (el.focus) el.focus();
    const sel = window.getSelection();
    if (sel) {
      const r = document.createRange();
      r.selectNodeContents(el);
      r.collapse(false);
      sel.removeAllRanges();
      sel.addRange(r);
    }
  }

  function paste(target, html, text) {
    const dt = new DataTransfer();
    if (html) dt.setData("text/html", html);
    dt.setData("text/plain", text || "");
    const ev = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt });
    const active = document.activeElement;
    (active && active !== document.body ? active : target).dispatchEvent(ev);
  }

  async function insertTitle(p) {
    const el = titleEl();
    if (!el) return false;
    focusEnd(el);
    await sleep(200);
    paste(el, "", p.title);
    await sleep(600);
    const box = el.closest(SEL.titleBox) || el;
    return norm(box.textContent).includes(norm(p.title).slice(0, 15));
  }

  async function insertBody(p) {
    const el = bodyEl();
    if (!el) return false;
    const before = norm(document.querySelector(SEL.editor[0])?.textContent || "").length;
    focusEnd(el);
    await sleep(200);
    paste(el, p.html, p.text);
    await sleep(1500);
    const after = norm(document.querySelector(SEL.editor[0])?.textContent || "");
    const probe = norm(p.text).slice(0, 12);
    return after.length > before + 20 && (!probe || after.includes(probe));
  }

  async function copyToClipboard(p, withHtml) {
    try {
      const items = { "text/plain": new Blob([p.text], { type: "text/plain" }) };
      if (withHtml) items["text/html"] = new Blob([p.html], { type: "text/html" });
      await navigator.clipboard.write([new ClipboardItem(items)]);
      return true;
    } catch (e) {
      try { await navigator.clipboard.writeText(p.text); return true; } catch (e2) { return false; }
    }
  }

  // ------------------------------------------------------------ 안내 배너 (페이지 스타일과 섞이지 않게 Shadow DOM)
  function banner(p) {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;top:70px;right:16px;z-index:2147483647;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>
      .b{width:300px;background:#fff;color:#222;border:2px solid #03c75a;border-radius:12px;padding:12px 14px;
         font:13px/1.5 'Malgun Gothic',sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.18)}
      h4{margin:0 0 6px;font-size:14px}.s{margin:6px 0 10px;color:#444;white-space:pre-line}
      button{font:inherit;cursor:pointer;border:1px solid #03c75a;background:#03c75a;color:#fff;border-radius:7px;padding:5px 9px;margin:2px}
      button.g{background:#fff;color:#222;border-color:#ccc}.x{float:right;border:0;background:none;color:#888;font-size:16px;padding:0}
      .t{color:#666;font-size:12px;margin-top:6px}</style>
      <div class="b"><button class="x" id="x" title="닫기">×</button><h4>✍️ BlogScope 원고</h4>
      <div class="t" id="tt"></div><div class="s" id="s">준비 중…</div>
      <button id="bt">제목 넣기</button><button id="bb">본문 넣기</button><br>
      <button class="g" id="cb">본문 복사</button><button class="g" id="ct">태그 복사</button>
      <div class="t">내용을 확인한 뒤 오른쪽 위 <b>[발행]</b>을 직접 눌러주세요. 태그는 발행 창에 붙여넣으세요.</div></div>`;
    document.documentElement.appendChild(host);
    const $ = (id) => root.getElementById(id);
    $("tt").textContent = "제목: " + p.title;
    const status = (t) => { $("s").textContent = t; };
    $("x").onclick = () => host.remove();
    $("bt").onclick = async () => status((await insertTitle(p)) ? "✔ 제목을 넣었습니다." :
      "제목 칸을 찾지 못했습니다. 제목 칸을 클릭한 뒤 직접 입력해 주세요.");
    $("bb").onclick = async () => status((await insertBody(p)) ? "✔ 본문을 넣었습니다. 사진 자리([사진: …])에 사진을 넣어주세요." :
      "자동 입력이 안 됐습니다. [본문 복사]를 누르고 본문을 클릭한 뒤 Ctrl+V 하세요.");
    $("cb").onclick = async () => status((await copyToClipboard(p, true)) ? "본문을 복사했습니다. 본문을 클릭하고 Ctrl+V 하세요." :
      "복사에 실패했습니다. BlogScope 화면에서 복사 버튼을 이용하세요.");
    $("ct").onclick = async () => {
      const tags = (p.tags || []).map((t) => "#" + t).join(" ");
      try { await navigator.clipboard.writeText(tags); status("태그를 복사했습니다: " + tags); }
      catch (e) { status("태그: " + tags); }
    };
    return status;
  }

  async function main() {
    let st;
    try { st = await chrome.storage.local.get("pending"); } catch (e) { return; }
    const pend = st && st.pending;
    if (!pend || !pend.payload || Date.now() - pend.ts > MAX_AGE) return;
    // 에디터가 있는 프레임에서만 동작
    const found = await waitFor(() => titleEl() && first(SEL.editor), 60000);
    if (!found) return;
    const p = pend.payload;
    const status = banner(p);
    if (pend.used) { status("이미 한 번 넣은 원고입니다. 필요하면 버튼으로 다시 넣으세요."); return; }
    await chrome.storage.local.set({ pending: { ...pend, used: true } });
    await sleep(1500);
    if (document.querySelector(SEL.popup)) {
      status("네이버 안내 창(예: 작성 중인 글)을 먼저 닫은 뒤 [제목 넣기] → [본문 넣기]를 눌러주세요.");
      return;
    }
    const t = await insertTitle(p);
    const b = await insertBody(p);
    status(t && b ? "✔ 제목과 본문을 넣었습니다.\n사진 자리에 사진을 넣고, 확인 후 [발행]을 눌러주세요."
      : (t ? "✔ 제목은 넣었습니다. " : "제목 자동 입력 실패. ") +
        (b ? "✔ 본문은 넣었습니다." : "본문 자동 입력 실패 → [본문 복사] 후 본문 클릭, Ctrl+V"));
  }

  main();
})();
