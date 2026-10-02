// BlogScope 페이지(bridge.js)에서 받은 원고를 저장하고 네이버 글쓰기 창을 연다.
const WRITE_URL = (blogId) =>
  blogId && /^[A-Za-z0-9_-]{2,40}$/.test(blogId)
    ? `https://blog.naver.com/${blogId}/postwrite`
    : "https://blog.naver.com/GoBlogWrite.naver";

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "blogscope-send" && msg.payload) {
    const p = msg.payload;
    const payload = {
      title: String(p.title || "").slice(0, 200),
      html: String(p.html || "").slice(0, 400000),
      text: String(p.text || "").slice(0, 200000),
      tags: Array.isArray(p.tags) ? p.tags.map(String).slice(0, 30) : [],
      blogId: String(p.blogId || ""),
    };
    chrome.storage.local.set({ pending: { payload, ts: Date.now(), used: false } }, () => {
      chrome.tabs.create({ url: WRITE_URL(payload.blogId) });
      reply({ ok: true });
    });
    return true; // 비동기 응답
  }
});
