/* 공통 종목 메뉴 모달 — 종목코드·종목명을 누르면 뜬다.
   세력추정매매(dashboard.html)와 수급포착매매(supply.html)가 함께 쓴다.
     · 토스로 보기          — 토스증권 종목 페이지를 새 창으로
     · 반전 매매에 종목추가  — POST /api/bollinger/symbols (봇을 시작하지는 않는다)
   페이지는 stockLink(r, text, cls) 로 셀을 만들고, 행 클릭 핸들러에서
   StockMenu.fromEvent(ev, symbol, name) 이 true 면 상세 열기를 건너뛴다. */
(function () {
  const $ = (s) => document.querySelector(s);
  const tossUrl = (symbol) => `https://www.tossinvest.com/stocks/${encodeURIComponent(symbol)}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ITEM = "block w-full text-left px-3.5 py-2.5 text-sm rounded-[10px] border " +
               "border-line bg-s0 text-txt no-underline hover:border-accent hover:text-accent " +
               "disabled:opacity-50 disabled:cursor-not-allowed";
  const SUB = "block mt-0.5 text-[11.5px] text-txt3";

  const MODAL_HTML = `<div class="${UI.cls.modalBack}" id="stock-menu" hidden role="dialog" aria-modal="true" aria-labelledby="sm-title">
  <div class="${UI.cls.modal} !w-[min(360px,100%)]">
    <h2 id="sm-title" class="m-0 mb-1 text-[17px] font-semibold"></h2>
    <p id="sm-code" class="m-0 mb-3.5 text-[12.5px] text-txt3 tabular-nums"></p>
    <div class="grid gap-2">
      <a id="sm-toss" href="#" target="_blank" rel="noopener noreferrer" class="${ITEM}">토스로 보기
        <small class="${SUB}">토스증권 종목 페이지를 새 창으로 엽니다</small></a>
      <button type="button" id="sm-bb" class="${ITEM}">반전 매매에 종목추가
        <small class="${SUB}">볼린저 밴드 반전 매매 감시 목록에 넣습니다</small></button>
    </div>
    <div id="sm-msg" role="status" class="text-[12.5px] mt-2.5"></div>
    <div class="flex flex-wrap gap-2 items-center mt-3.5">
      <div class="flex-1"></div>
      <button type="button" id="sm-close" class="${UI.cls.btn}">닫기</button>
    </div>
  </div>
</div>`;

  let current = null, opener = null, box = null;
  const TONE = { bad: "text-bad", ok: "text-good", "": "text-txt2" };
  const say = (text, kind = "") => {
    const m = $("#sm-msg");
    m.textContent = text;
    m.className = `text-[12.5px] mt-2.5 ${TONE[kind] || TONE[""]}`;
  };

  function open(symbol, name, from) {
    if (!box) return;
    current = { symbol, name };
    opener = from || null;
    $("#sm-title").textContent = name || symbol;
    $("#sm-code").textContent = symbol;
    $("#sm-toss").href = tossUrl(symbol);
    $("#sm-bb").disabled = false;
    say("");
    box.hidden = false;
    $("#sm-toss").focus();
  }

  function close() {
    if (!box) return;
    box.hidden = true;
    if (opener && document.contains(opener)) opener.focus();
  }

  async function addToBollinger() {
    if (!current) return;
    const btn = $("#sm-bb");
    btn.disabled = true;
    say("추가하는 중…");
    try {
      const r = await fetch("/api/bollinger/symbols", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: current.symbol }) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { say(body.detail || `추가하지 못했습니다 (HTTP ${r.status})`, "bad"); btn.disabled = false; return; }
      const n = body.symbols.length;
      const head = body.added ? `감시 목록에 추가했습니다 (총 ${n}종목).`
                              : `이미 감시 목록에 있습니다 (총 ${n}종목).`;
      const tail = body.running ? " 실행 중인 봇이 다음 주기부터 감시합니다."
                                : " 봇은 반전 매매 화면에서 시작하세요.";
      say(head + tail, "ok");
    } catch (e) {
      say("요청 실패: " + e.message, "bad");
      btn.disabled = false;
    }
  }

  /* 표 셀용 버튼 마크업 */
  function link(r, text, cls = "") {
    const base = "sf-stock bg-transparent border-0 p-0 text-left text-inherit cursor-pointer " +
                 "underline decoration-dotted decoration-txt3 underline-offset-[3px] " +
                 "hover:text-accent hover:decoration-accent";
    return `<button type="button" class="${base} ${cls}" data-stock="${esc(r["종목코드"])}"
       title="${esc(r["종목명"])}(${esc(r["종목코드"])}) 메뉴 열기">${esc(text)}</button>`;
  }

  /* 행 클릭 이벤트가 종목 버튼에서 왔으면 메뉴를 열고 true */
  function fromEvent(ev, symbol, name) {
    const btn = ev.target.closest("button.sf-stock");
    if (!btn) return false;
    open(symbol, name, btn);
    return true;
  }

  function mount() {
    const holder = document.createElement("div");
    holder.innerHTML = MODAL_HTML;
    box = holder.firstElementChild;
    document.body.appendChild(box);
    $("#sm-close").onclick = close;
    $("#sm-bb").onclick = addToBollinger;
    $("#sm-toss").addEventListener("click", close);
    box.addEventListener("click", ev => { if (ev.target === box) close(); });
    document.addEventListener("keydown", ev => {
      if (ev.key === "Escape" && box && !box.hidden) close();
    });
  }

  window.StockMenu = { open, close, link, fromEvent, tossUrl };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
