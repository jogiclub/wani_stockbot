/* 공통 종목 메뉴 모달 — 종목코드·종목명을 누르면 뜬다.
   세력추정매매(dashboard.html)에서 쓴다. (Bootstrap Modal)
     · 토스로 보기          — 토스증권 종목 페이지를 새 창으로
     · 반전 매매에 종목추가  — POST /api/bollinger/symbols (봇을 시작하지는 않는다)
   페이지는 stockLink(r, text, cls) 로 셀을 만들고, 행 클릭 핸들러에서
   StockMenu.fromEvent(ev, symbol, name) 이 true 면 상세 열기를 건너뛴다. */
(function () {
  const $ = (s) => document.querySelector(s);
  const tossUrl = (symbol) => `https://www.tossinvest.com/stocks/${encodeURIComponent(symbol)}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ITEM = "btn btn-outline-secondary text-start w-100 py-2";
  const SUB = "d-block small text-body-tertiary fw-normal";

  const MODAL_HTML = `<div class="modal fade" id="stock-menu" tabindex="-1" aria-labelledby="sm-title" aria-hidden="true">
  <div class="modal-dialog modal-sm modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <div>
          <h2 id="sm-title" class="modal-title fs-5 fw-semibold"></h2>
          <div id="sm-code" class="small text-body-tertiary tnum"></div>
        </div>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="닫기"></button>
      </div>
      <div class="modal-body">
        <div class="d-grid gap-2">
          <a id="sm-toss" href="#" target="_blank" rel="noopener noreferrer" class="${ITEM}">토스로 보기
            <small class="${SUB}">토스증권 종목 페이지를 새 창으로 엽니다</small></a>
          <button type="button" id="sm-bb" class="${ITEM}">반전 매매에 종목추가
            <small class="${SUB}">볼린저 밴드 반전 매매 감시 목록에 넣습니다</small></button>
        </div>
        <div id="sm-msg" role="status" class="small mt-2"></div>
      </div>
      <div class="modal-footer">
        <button type="button" id="sm-close" class="${UI.cls.btn}" data-bs-dismiss="modal">닫기</button>
      </div>
    </div>
  </div>
</div>`;

  let current = null, opener = null, box = null, modal = null;
  const TONE = { bad: "text-bad", ok: "text-good", "": "text-body-secondary" };
  const say = (text, kind = "") => {
    const m = $("#sm-msg");
    m.textContent = text;
    m.className = `small mt-2 ${TONE[kind] || TONE[""]}`;
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
    modal.show();
  }

  function close() {
    if (!box) return;
    modal.hide();
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
    const base = "stock";
    return `<button type="button" class="${base} ${cls}" data-stock="${esc(r["종목코드"])}"
       title="${esc(r["종목명"])}(${esc(r["종목코드"])}) 메뉴 열기">${esc(text)}</button>`;
  }

  /* 행 클릭 이벤트가 종목 버튼에서 왔으면 메뉴를 열고 true */
  function fromEvent(ev, symbol, name) {
    const btn = ev.target.closest("button.stock");
    if (!btn) return false;
    open(symbol, name, btn);
    return true;
  }

  function mount() {
    const holder = document.createElement("div");
    holder.innerHTML = MODAL_HTML;
    box = holder.firstElementChild;
    document.body.appendChild(box);
    modal = bootstrap.Modal.getOrCreateInstance(box);
    box.addEventListener("shown.bs.modal", () => $("#sm-toss").focus());
    box.addEventListener("hidden.bs.modal", () => {
      if (opener && document.contains(opener)) opener.focus();
    });
    $("#sm-bb").onclick = addToBollinger;
    $("#sm-toss").addEventListener("click", close);
  }

  window.StockMenu = { open, close, link, fromEvent, tossUrl };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
