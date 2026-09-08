/* 개인 토스증권 API 키 설정 (세션) — 두 화면이 함께 쓰는 공통 모듈.

   키는 서버 메모리에만 남고 브라우저는 세션 쿠키(sf_session)만 갖는다.
   화면에는 마스킹된 client_id 만 돌아오므로 secret 을 localStorage 등에 저장하지 않는다.

   페이지에서 쓰는 것
     Session.load()          서버에서 세션 정보를 다시 읽는다 (Promise)
     Session.open()          설정 창 열기
     Session.info            마지막으로 읽은 세션 정보 (없으면 null)
     "sessionchange" 이벤트   저장·해제 후 페이지가 자기 상태를 새로고침할 때 듣는다

   페이지 준비물
     - GNB 로 옮겨질 설정 버튼: <button data-settings-btn data-gnb-action>⚙ 설정</button>
     - (선택) 안내 바: <div id="setup" class="setup-bar" hidden></div> */
(function () {
  const MODAL_HTML = `<div class="modal-back" id="modal" hidden role="dialog" aria-modal="true" aria-labelledby="modal-title">
  <div class="modal">
    <h2 id="modal-title">내 토스증권 API 키</h2>
    <p class="sub">입력한 키는 <b>이 브라우저 세션 동안 서버 메모리에만</b> 보관되며 파일에 저장하지
      않습니다. 키는 <a class="toss" href="https://developers.tossinvest.com" target="_blank"
      rel="noopener noreferrer">developers.tossinvest.com</a> 에서 발급하고,
      접속 서버 IP 를 허용 IP 로 등록해야 합니다.</p>

    <fieldset>
      <legend>자격증명 (필수)</legend>
      <div class="field">
        <label for="f-id">Client ID</label>
        <input type="text" id="f-id" autocomplete="off" spellcheck="false" placeholder="tsck_live_…">
      </div>
      <div class="field">
        <label for="f-secret">Client Secret</label>
        <input type="password" id="f-secret" autocomplete="new-password" placeholder="tssk_live_…">
        <div class="hint">저장 시 실제 API 를 한 번 호출해 키가 유효한지 확인합니다.</div>
      </div>
    </fieldset>

    <fieldset>
      <legend>스캔 범위</legend>
      <div class="field-row">
        <div class="field">
          <label for="f-top">거래대금 상위 N종목</label>
          <input type="number" id="f-top" min="0" max="1000" step="10">
          <div class="hint">0 이면 전체 유니버스 (느립니다)</div>
        </div>
        <div class="field">
          <label for="f-interval">스캔 주기(초)</label>
          <input type="number" id="f-interval" min="30" max="3600" step="30">
          <div class="hint">30~3600초</div>
        </div>
      </div>
      <div class="field check">
        <input type="checkbox" id="f-live"><label for="f-live" style="margin:0">장중 실시간 현재가로 돌파 판정</label>
      </div>
    </fieldset>

    <fieldset>
      <legend>텔레그램 알림 (선택)</legend>
      <div class="field-row">
        <div class="field">
          <label for="f-tg-token">Bot Token</label>
          <input type="password" id="f-tg-token" autocomplete="off">
        </div>
        <div class="field">
          <label for="f-tg-chat">Chat ID</label>
          <input type="text" id="f-tg-chat" autocomplete="off">
        </div>
      </div>
    </fieldset>

    <fieldset id="f-trade-set">
      <legend>주문 (선택)</legend>
      <div class="field-row">
        <div class="field">
          <label for="f-account">계좌 번호(accountSeq)</label>
          <input type="number" id="f-account" min="0" step="1">
          <div class="hint">비우면 첫 번째 계좌</div>
        </div>
        <div class="field">
          <label for="f-max">1회 주문 한도(원)</label>
          <input type="number" id="f-max" min="0" step="100000">
          <div class="hint" id="f-max-hint"></div>
        </div>
      </div>
      <div class="field check" id="f-optin-row" hidden>
        <input type="checkbox" id="f-optin">
        <label for="f-optin" style="margin:0">⚠ 실주문 허용 (끄면 모의 주문만)</label>
      </div>
    </fieldset>

    <div class="modal-actions">
      <button class="primary" id="f-save">저장하고 시작</button>
      <button id="f-close">닫기</button>
      <div class="spacer"></div>
      <button id="f-clear" hidden>연결 해제</button>
    </div>
    <div class="modal-msg" id="f-msg"></div>
  </div>
</div>`;

  const $ = (s) => document.querySelector(s);
  const fmt = (n) => n == null || Number.isNaN(n) ? "–"
    : Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });

  let SESSION = null;
  let modal = null;

  function fire() {
    document.dispatchEvent(new CustomEvent("sessionchange", { detail: SESSION }));
  }

  async function loadSession() {
    try {
      SESSION = await (await fetch("/api/session")).json();
    } catch (e) {
      SESSION = null;
      return null;
    }
    const s = SESSION;
    // 개인 키 입력을 지원하지 않는 서버(합성·CSV 소스)에서는 설정 버튼 자체를 감춘다.
    document.querySelectorAll("[data-settings-btn]").forEach(btn => {
      btn.hidden = !s.multi_user;
      btn.textContent = s.source === "session" ? "⚙ 내 API 키" : "⚙ 설정";
    });
    $("#f-clear").hidden = s.source !== "session";
    $("#f-optin-row").hidden = !s.trading_allowed;
    $("#f-max-hint").textContent = s.server_max_order_amount
      ? `서버 상한 ${fmt(s.server_max_order_amount)}원 이내` : "";
    $("#f-trade-set").hidden = !s.multi_user;

    // 이미 세션이 있으면 입력값을 현재 설정으로 채운다 (secret 은 서버가 주지 않는다).
    $("#f-id").value = "";
    $("#f-id").placeholder = s.client_id_masked || "tsck_live_…";
    $("#f-top").value = s.top ?? 100;
    $("#f-interval").value = s.interval ?? 300;
    $("#f-live").checked = !!s.live;
    $("#f-account").value = s.account_seq ?? "";
    $("#f-max").value = s.max_order_amount ?? s.server_max_order_amount ?? 1000000;
    $("#f-optin").checked = !!s.trading_opt_in;

    renderSetupBar();
    if (!s.configured && s.multi_user) openModal();
    return s;
  }

  function renderSetupBar() {
    const bar = $("#setup"), s = SESSION;
    if (!bar) return;
    if (!s || s.configured) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = s.multi_user
      ? `<b>토스증권 API 키가 필요합니다.</b><span>상단 <b>⚙ 설정</b> 에서 본인 키를 입력하면 시작됩니다.</span>`
      : `<b>서버에 API 키가 설정되지 않았습니다.</b><span>서버 <code>.env</code> 를 확인하세요.</span>`;
  }

  function openModal() {
    if (!modal) return;
    modal.hidden = false;
    $("#f-msg").textContent = "";
    setTimeout(() => $("#f-id").focus(), 30);
  }
  function closeModal() { if (modal) modal.hidden = true; }

  function msg(text, kind = "") {
    const el = $("#f-msg");
    el.className = "modal-msg " + kind;
    el.textContent = text;
  }

  function bindForm() {
    $("#f-save").onclick = async () => {
      const body = {
        client_id: $("#f-id").value.trim(),
        client_secret: $("#f-secret").value.trim(),
        top: Number($("#f-top").value || 0),
        interval: Number($("#f-interval").value || 300),
        live: $("#f-live").checked,
        account_seq: Number($("#f-account").value || 0),
        max_order_amount: Number($("#f-max").value || 0),
        trading_opt_in: $("#f-optin").checked,
        telegram_bot_token: $("#f-tg-token").value.trim(),
        telegram_chat_id: $("#f-tg-chat").value.trim(),
      };
      if (!body.client_id || !body.client_secret) return msg("Client ID 와 Secret 을 입력하세요.", "bad");
      $("#f-save").disabled = true;
      msg("키를 확인하는 중…");
      try {
        const r = await fetch("/api/session", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const out = await r.json();
        if (!r.ok) { msg(out.detail || "설정 실패", "bad"); return; }
        $("#f-secret").value = ""; $("#f-tg-token").value = "";
        msg("연결됐습니다.", "ok");
        await loadSession();
        fire();
        setTimeout(closeModal, 700);
      } catch (e) {
        msg("서버에 연결하지 못했습니다: " + e, "bad");
      } finally {
        $("#f-save").disabled = false;
      }
    };

    $("#f-clear").onclick = async () => {
      if (!confirm("저장된 API 키 연결을 해제할까요? 진행 중인 개인 스캔·봇이 중단됩니다.")) return;
      await fetch("/api/session", { method: "DELETE" });
      msg("연결을 해제했습니다.", "ok");
      await loadSession();
      fire();
    };
  }

  function mount() {
    const holder = document.createElement("div");
    holder.innerHTML = MODAL_HTML;
    document.body.appendChild(holder.firstElementChild);
    modal = $("#modal");

    $("#f-close").onclick = closeModal;
    modal.addEventListener("click", ev => { if (ev.target === modal) closeModal(); });
    document.addEventListener("keydown", ev => {
      if (ev.key === "Escape" && modal && !modal.hidden) closeModal();
    });
    document.querySelectorAll("[data-settings-btn]").forEach(btn => {
      btn.onclick = () => loadSession().then(openModal);
    });
    bindForm();
    loadSession().then(fire);
  }

  window.Session = {
    load: loadSession,
    open: openModal,
    get info() { return SESSION; },
  };
  document.addEventListener("DOMContentLoaded", mount);
})();
