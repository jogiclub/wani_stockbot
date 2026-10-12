/* 개인 토스증권 API 키(🔑) + 그 밖의 설정(⚙) — 세 화면이 함께 쓰는 공통 모듈.

   창을 둘로 나눈 이유: 설정을 조금 바꿀 때마다 Client ID·Secret 을 다시 입력해야 했다.
     🔑 내 API 키 — 자격증명만 (`POST /api/session`). 나머지 설정은 저장값을 이어받는다.
     ⚙ 설정     — 스캔 범위·알림·주문·자동매수 (`POST /api/session/settings`).
                  서버가 저장된 키로 세션을 다시 열기 때문에 키 입력이 필요 없다.

   키는 서버 메모리·DB(암호화)에만 남고 브라우저는 세션 쿠키(sf_session)만 갖는다.
   화면에는 마스킹된 client_id 만 돌아오므로 secret 을 localStorage 등에 저장하지 않는다.

   페이지에서 쓰는 것
     Session.load()          서버에서 세션 정보를 다시 읽는다 (Promise)
     Session.open()          설정 창 열기 / Session.openKeys() API 키 창 열기
     Session.info            마지막으로 읽은 세션 정보 (없으면 null)
     "sessionchange" 이벤트   저장·해제 후 페이지가 자기 상태를 새로고침할 때 듣는다

   페이지 준비물
     - GNB 로 옮겨질 설정 버튼: <button data-settings-btn data-gnb-action>⚙ 설정</button>
       (API 키 버튼은 이 모듈이 그 옆에 만들어 넣는다)
     - (선택) 안내 바: <div id="setup" class="setup-bar" hidden></div> */
(function () {
  const c = UI.cls;
  const MSG = "small mt-2";
  const SETUP_BAR = "tint-accent border border-accent px-3 py-2 rounded-3 mb-3 " +
                    "small d-flex flex-wrap gap-2 align-items-center";
  const CHECK = "form-check mt-2";
  const HEAD = (id, title) => `<div class="modal-header">
        <h2 id="${id}" class="modal-title fs-5 fw-semibold">${title}</h2>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="닫기"></button>
      </div>`;
  const KEYS_HTML = `<div class="modal fade" id="keys-modal" tabindex="-1" aria-labelledby="keys-title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-scrollable">
    <div class="modal-content">
      ${HEAD("keys-title", "내 토스증권 API 키")}
      <div class="modal-body">
        <p class="small text-body-tertiary">로그인 계정에 저장되며 <b>secret 은 암호화</b>해 보관합니다.
          서버를 재시작해도 다시 입력할 필요가 없습니다. 키는 <a class="text-accent" href="https://developers.tossinvest.com" target="_blank"
          rel="noopener noreferrer">developers.tossinvest.com</a> 에서 발급하고,
          접속 서버 IP 를 허용 IP 로 등록해야 합니다.</p>

        <fieldset class="${c.fieldset}">
          <legend class="${c.legend}">자격증명</legend>
          <div class="mb-2">
            <label class="${c.label}" for="f-id">Client ID</label>
            <input class="${c.input}" type="text" id="f-id" autocomplete="off" spellcheck="false" placeholder="tsck_live_…">
          </div>
          <div class="mb-2">
            <label class="${c.label}" for="f-secret">Client Secret</label>
            <input class="${c.input}" type="password" id="f-secret" autocomplete="new-password" placeholder="tssk_live_…">
            <div class="${c.hint}">저장 시 실제 API 를 한 번 호출해 키가 유효한지 확인합니다.
              스캔·알림·주문 설정은 <b>⚙ 설정</b> 창에 있으며 그대로 유지됩니다.</div>
          </div>
        </fieldset>
        <div class="${MSG}" id="f-msg"></div>
      </div>
      <div class="modal-footer justify-content-start">
        <button class="${c.btnPrimary}" id="f-save">저장하고 시작</button>
        <button class="${c.btn}" id="f-close" data-bs-dismiss="modal">닫기</button>
        <div class="flex-grow-1"></div>
        <button class="${c.btnDanger}" id="f-clear" hidden>연결 해제</button>
      </div>
    </div>
  </div>
</div>`;

  const PREFS_HTML = `<div class="modal fade" id="modal" tabindex="-1" aria-labelledby="modal-title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-scrollable modal-lg">
    <div class="modal-content">
      ${HEAD("modal-title", "설정")}
      <div class="modal-body">
        <p class="small text-body-tertiary">저장하면 개인 스캔이 새 설정으로 다시 시작됩니다
          (실행 중이던 볼린저 봇은 멈추므로 다시 시작해야 합니다).
          API 키는 <b>🔑 내 API 키</b> 창에서 바꿉니다.</p>

        <fieldset class="${c.fieldset}">
          <legend class="${c.legend}">스캔 범위</legend>
          <div class="row g-2">
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-top">거래대금 상위 N종목</label>
              <input class="${c.input}" type="number" id="f-top" min="0" max="1000" step="10">
              <div class="${c.hint}">0 이면 전체 유니버스 (느립니다)</div>
            </div>
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-interval">스캔 주기(초)</label>
              <input class="${c.input}" type="number" id="f-interval" min="30" max="3600" step="30">
              <div class="${c.hint}">30~3600초</div>
            </div>
          </div>
          <div class="${CHECK}">
            <input class="form-check-input" type="checkbox" id="f-live"><label class="form-check-label small" for="f-live">장중 실시간 현재가로 돌파 판정</label>
          </div>
        </fieldset>

        <fieldset class="${c.fieldset}">
          <legend class="${c.legend}">텔레그램 알림 (선택)</legend>
          <div class="row g-2">
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-tg-token">Bot Token</label>
              <input class="${c.input}" type="password" id="f-tg-token" autocomplete="off" placeholder="123456789:AA…">
              <div class="${c.hint}">BotFather 가 준 문자열 <b>전체</b> (콜론 포함).
                비워 두면 저장값을 그대로 씁니다.</div>
            </div>
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-tg-chat">Chat ID</label>
              <input class="${c.input}" type="text" id="f-tg-chat" autocomplete="off" placeholder="123456789">
              <div class="${c.hint}">대화방 <b>숫자</b> ID. 토큰의 일부가 아닙니다.</div>
            </div>
          </div>
          <div class="d-flex align-items-center gap-2 flex-wrap mt-2">
            <button class="${c.btn}" id="f-tg-test">테스트 전송</button>
            <span class="small text-body-tertiary" id="f-tg-note">봇에게 먼저 아무 메시지나 보낸 뒤 눌러보세요.</span>
          </div>
        </fieldset>

        <fieldset id="f-email-set" class="${c.fieldset}">
          <legend class="${c.legend}">이메일 알림 (선택)</legend>
          <div class="mb-2">
            <label class="${c.label}" for="f-emails">받는 사람 이메일 — 세력추정매매 새 시그널</label>
            <textarea class="${c.input}" style="height:80px;resize:vertical" id="f-emails" autocomplete="off"
              placeholder="여러 개면 콤마·줄바꿈으로 구분 (예: a@a.com, b@b.com)"></textarea>
            <div class="${c.hint}" id="f-email-hint">비워 두면 이메일을 보내지 않습니다.</div>
          </div>
          <div class="d-flex align-items-center gap-2 flex-wrap mt-2">
            <button class="${c.btn}" id="f-em-test">테스트 전송</button>
            <span class="small text-body-tertiary" id="f-em-note"></span>
          </div>
        </fieldset>

        <fieldset id="f-trade-set" class="${c.fieldset}">
          <legend class="${c.legend}">주문 (선택)</legend>
          <div class="row g-2">
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-account">계좌 번호(accountSeq)</label>
              <input class="${c.input}" type="number" id="f-account" min="0" step="1">
              <div class="${c.hint}">비우면 첫 번째 계좌</div>
            </div>
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-max">1회 주문 한도(원)</label>
              <input class="${c.input}" type="number" id="f-max" min="0" step="100000">
              <div class="${c.hint}" id="f-max-hint"></div>
            </div>
          </div>
          <div class="${CHECK}" id="f-optin-row" hidden>
            <input class="form-check-input" type="checkbox" id="f-optin">
            <label class="form-check-label small" for="f-optin">⚠ 실주문 허용 (끄면 모의 주문만)</label>
          </div>
          <div class="${CHECK}">
            <input class="form-check-input" type="checkbox" id="f-auto">
            <label class="form-check-label small" for="f-auto">세력추정매매 시그널 자동 매수</label>
          </div>
          <div class="row g-2 mt-1">
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-auto-score">점수 이상은 토스 자동매수</label>
              <input class="${c.input}" type="number" id="f-auto-score" min="0" max="100" step="1" placeholder="75">
              <div class="${c.hint}">실측 컷오프 75점 (그 아래는 승률 50% 안팎)</div>
            </div>
            <div class="col-12 col-sm-6">
              <label class="${c.label}" for="f-auto-amount">1종목 매수 금액(원)</label>
              <input class="${c.input}" type="number" id="f-auto-amount" min="0" step="100000" placeholder="500000">
              <div class="${c.hint}">이 금액 안에서 최대 수량 (1회 한도·예수금도 넘지 않음)</div>
            </div>
          </div>
          <div class="${c.hint} mt-2" id="f-auto-hint">장중(09:00~15:20) 스캔에서 조건을 넘은 종목을 하루 1회,
            매수가(전일 고가) 지정가로 주문하고 체결되면 1R 익절·손절 OCO 를 겁니다.
            장 마감까지 미체결이면 취소합니다. 실주문 허용이 꺼져 있으면 모의로만 기록됩니다.</div>
        </fieldset>
        <div class="${MSG}" id="p-msg"></div>
      </div>
      <div class="modal-footer justify-content-start">
        <button class="${c.btnPrimary}" id="p-save">저장</button>
        <button class="${c.btn}" id="p-close" data-bs-dismiss="modal">닫기</button>
      </div>
    </div>
  </div>
</div>`;

  const $ = (s) => document.querySelector(s);
  const fmt = (n) => n == null || Number.isNaN(n) ? "–"
    : Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });

  let SESSION = null;
  let keysModal = null, prefsModal = null;

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
    // 개인 키 입력을 지원하지 않는 서버(합성·CSV 소스)에서는 버튼 자체를 감춘다.
    document.querySelectorAll("[data-settings-btn]").forEach(btn => btn.hidden = !s.multi_user);
    document.querySelectorAll("[data-keys-btn]").forEach(btn => {
      btn.hidden = !s.multi_user;
      btn.textContent = s.source === "session" ? "🔑 내 API 키" : "🔑 API 키 설정";
    });
    $("#f-clear").hidden = s.source !== "session";
    $("#f-optin-row").hidden = !s.trading_allowed;
    $("#f-max-hint").textContent = s.server_max_order_amount
      ? `서버 상한 ${fmt(s.server_max_order_amount)}원 이내` : "";
    $("#f-trade-set").hidden = !s.multi_user;

    // 저장된 설정으로 입력값을 채운다 (secret·봇 토큰은 서버가 주지 않는다).
    $("#f-id").value = "";
    $("#f-id").placeholder = s.client_id_masked || "tsck_live_…";
    $("#f-top").value = s.top ?? 100;
    $("#f-interval").value = s.interval ?? 300;
    $("#f-live").checked = !!s.live;
    $("#f-account").value = s.account_seq ?? "";
    $("#f-max").value = s.max_order_amount ?? s.server_max_order_amount ?? 1000000;
    $("#f-optin").checked = !!s.trading_opt_in;
    $("#f-emails").value = s.alert_emails || "";
    $("#f-email-set").hidden = !s.mail_enabled;      // 서버에 발신 계정이 없으면 숨긴다
    emNote(s.mail_enabled ? "" : "서버에 이메일 발신 계정이 없어 비활성화됩니다.",
           s.mail_enabled ? "" : "bad");
    $("#f-auto").checked = !!s.auto_buy;
    $("#f-auto-score").value = s.auto_buy_score ?? 75;
    $("#f-auto-amount").value = s.auto_buy_amount || "";
    tgNote(s.telegram ? "저장된 설정이 있습니다. 빈 칸으로 두고 테스트하면 저장값으로 보냅니다."
                      : "봇에게 먼저 아무 메시지나 보낸 뒤 눌러보세요.",
           s.telegram ? "ok" : "");

    renderSetupBar();
    if (!s.configured && s.multi_user) openKeys();
    return s;
  }

  function renderSetupBar() {
    const bar = $("#setup"), s = SESSION;
    if (!bar) return;
    if (s && s.source === "server" && s.multi_user && !s.is_admin) {
      // 일반 사용자 — 서버 키의 공용 시세 화면만 보는 중. 주문·봇은 본인 키가 있어야 한다.
      bar.hidden = false;
      bar.className = SETUP_BAR;
      bar.innerHTML = `<b>공용 시세 화면을 보고 있습니다.</b><span>상단 <b>🔑 API 키 설정</b> 에서 본인 토스증권 API 키를 넣으면 나만의 스캔·알림·자동매매를 쓸 수 있습니다.</span>`;
      return;
    }
    if (!s || s.configured) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.className = SETUP_BAR;
    bar.innerHTML = s.multi_user
      ? `<b>토스증권 API 키가 필요합니다.</b><span>상단 <b>🔑 API 키 설정</b> 에서 본인 키를 입력하면 시작됩니다.</span>`
      : `<b>서버에 API 키가 설정되지 않았습니다.</b><span>서버 <code>.env</code> 를 확인하세요.</span>`;
  }

  function openKeys() {
    if (!keysModal) return;
    keysModal.show();
    msg("");
    setTimeout(() => $("#f-id").focus(), 300);
  }
  function openPrefs() {
    if (!prefsModal) return;
    if (SESSION && SESSION.source !== "session") {
      // 저장된 키가 없으면 설정을 저장할 수 없다 — 키 창을 먼저 연다.
      openKeys();
      msg("먼저 API 키를 등록하세요. 설정은 그 다음에 저장할 수 있습니다.");
      return;
    }
    prefsModal.show();
    pmsg("");
  }
  const closeKeys = () => { if (keysModal) keysModal.hide(); };
  const closePrefs = () => { if (prefsModal) prefsModal.hide(); };

  const TONE = { bad: "text-bad", ok: "text-good", "": "text-body-secondary" };

  function msg(text, kind = "") { note("#f-msg", MSG, text, kind); }
  function pmsg(text, kind = "") { note("#p-msg", MSG, text, kind); }
  function tgNote(text, kind = "") { note("#f-tg-note", "small", text, kind); }
  function emNote(text, kind = "") { note("#f-em-note", "small", text, kind); }

  function note(sel, base, text, kind) {
    const el = $(sel);
    if (!el) return;
    el.className = `${base} ${TONE[kind] || (base === MSG ? TONE[""] : "text-body-tertiary")}`;
    el.textContent = text;
  }

  function bindForm() {
    // 저장 전에 실제로 1건 보내본다 — 토큰/챗ID 를 거꾸로 넣는 실수를 바로 잡아준다.
    $("#f-tg-test").onclick = async () => {
      const btn = $("#f-tg-test");
      btn.disabled = true;
      tgNote("전송 중…");
      try {
        const r = await fetch("/api/session/telegram/test", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            telegram_bot_token: $("#f-tg-token").value.trim(),
            telegram_chat_id: $("#f-tg-chat").value.trim(),
          }),
        });
        const out = await r.json();
        if (r.ok) tgNote(`전송 성공 — ${out.chat} 대화방을 확인하세요.`, "ok");
        else tgNote(out.detail || "전송 실패", "bad");
      } catch (e) {
        tgNote("서버에 연결하지 못했습니다: " + e, "bad");
      } finally {
        btn.disabled = false;
      }
    };

    // 🔑 API 키 — 자격증명만 보낸다. 나머지 설정은 서버가 저장값을 이어받는다.
    $("#f-em-test").onclick = async () => {
      const btn = $("#f-em-test");
      btn.disabled = true;
      emNote("전송 중…");
      try {
        const r = await fetch("/api/session/email/test", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alert_emails: $("#f-emails").value.trim() }),
        });
        const out = await r.json();
        if (r.ok) emNote(`전송 성공 — ${out.sent} 받은편지함(스팸함 포함)을 확인하세요.`, "ok");
        else emNote(out.detail || "전송 실패", "bad");
      } catch (e) {
        emNote("서버에 연결하지 못했습니다: " + e, "bad");
      } finally {
        btn.disabled = false;
      }
    };

    $("#f-save").onclick = async () => {
      const body = {
        client_id: $("#f-id").value.trim(),
        client_secret: $("#f-secret").value.trim(),
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
        $("#f-secret").value = "";
        msg(out.persisted === false ? "연결됐습니다." : "연결됐습니다. 키를 계정에 저장했습니다.", "ok");
        await loadSession();
        fire();
        setTimeout(closeKeys, 700);
      } catch (e) {
        msg("서버에 연결하지 못했습니다: " + e, "bad");
      } finally {
        $("#f-save").disabled = false;
      }
    };

    // ⚙ 설정 — 키 없이 저장. 서버가 저장된 키로 세션을 다시 연다.
    $("#p-save").onclick = async () => {
      const body = {
        top: Number($("#f-top").value || 0),
        interval: Number($("#f-interval").value || 300),
        live: $("#f-live").checked,
        account_seq: Number($("#f-account").value || 0),
        max_order_amount: Number($("#f-max").value || 0),
        trading_opt_in: $("#f-optin").checked,
        telegram_bot_token: $("#f-tg-token").value.trim(),
        telegram_chat_id: $("#f-tg-chat").value.trim(),
        alert_emails: $("#f-emails").value.trim(),
        auto_buy: $("#f-auto").checked,
        auto_buy_score: Number($("#f-auto-score").value || 75),
        auto_buy_amount: Number($("#f-auto-amount").value || 0),
      };
      if (body.auto_buy && body.auto_buy_amount <= 0) return pmsg("자동 매수 금액을 입력하세요.", "bad");
      if (body.auto_buy && body.trading_opt_in && !confirm(
          `${body.auto_buy_score}점 이상 시그널을 종목당 최대 ${fmt(body.auto_buy_amount)}원까지 `
          + "실제 계좌로 자동 매수합니다. 계속할까요?")) return;
      $("#p-save").disabled = true;
      pmsg("저장하는 중…");
      try {
        const r = await fetch("/api/session/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const out = await r.json();
        if (!r.ok) { pmsg(out.detail || "저장 실패", "bad"); return; }
        $("#f-tg-token").value = "";
        pmsg("저장했습니다. 새 설정으로 다시 시작했습니다.", "ok");
        await loadSession();
        fire();
        setTimeout(closePrefs, 700);
      } catch (e) {
        pmsg("서버에 연결하지 못했습니다: " + e, "bad");
      } finally {
        $("#p-save").disabled = false;
      }
    };

    $("#f-clear").onclick = async () => {
      if (!confirm("저장된 API 키 연결을 해제할까요? 서버에 저장된 설정도 함께 지워지고 "
                   + "진행 중인 개인 스캔·봇이 중단됩니다.")) return;
      await fetch("/api/session", { method: "DELETE" });
      msg("연결을 해제했습니다.", "ok");
      await loadSession();
      fire();
    };
  }

  function mount() {
    [KEYS_HTML, PREFS_HTML].forEach(html => {
      const holder = document.createElement("div");
      holder.innerHTML = html;
      document.body.appendChild(holder.firstElementChild);
    });
    // Bootstrap 번들은 body 끝에서 로드되지만 DOMContentLoaded 시점엔 이미 실행돼 있다.
    keysModal = bootstrap.Modal.getOrCreateInstance($("#keys-modal"));
    prefsModal = bootstrap.Modal.getOrCreateInstance($("#modal"));

    // 페이지에는 ⚙ 버튼만 있다. 🔑 버튼은 그 옆에 만들어 넣는다
    // (GNB 가 [data-gnb-action] 을 오른쪽으로 모으므로 같은 속성을 준다).
    document.querySelectorAll("[data-settings-btn]").forEach(btn => {
      btn.className = c.btn;
      btn.textContent = "⚙ 설정";
      btn.title = "스캔 범위 · 알림 · 주문 · 자동매수";
      btn.onclick = () => loadSession().then(openPrefs);
      const keys = document.createElement("button");
      keys.className = c.btn;
      keys.dataset.keysBtn = "";
      keys.dataset.gnbAction = "";
      keys.hidden = btn.hidden;
      keys.title = "토스증권 API 키";
      keys.textContent = "🔑 API 키 설정";
      keys.onclick = () => loadSession().then(openKeys);
      btn.parentNode.insertBefore(keys, btn);
    });
    bindForm();
    loadSession().then(fire);
  }

  window.Session = {
    load: loadSession,
    open: openPrefs,
    openKeys: openKeys,
    get info() { return SESSION; },
  };
  document.addEventListener("DOMContentLoaded", mount);
})();
