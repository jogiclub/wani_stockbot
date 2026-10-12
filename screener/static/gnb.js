/* 공통 GNB(전역 내비게이션) + 테마 토글 (Bootstrap 5.3).
   세 화면(세력추정매매·볼린저 반전 매매·금요일 마감 돌파)이 같은 마크업을 쓰도록 JS 로 한 번만 그린다.
   페이지는 [data-gnb-action] 버튼을 두면 오른쪽에 합쳐진다.
   테마는 <html data-bs-theme> — 이 스크립트가 <head> 에서 로드되는 시점에 바로 정해 깜빡임을 막는다. */
(function () {
  const MENU = [
    { href: "/", label: "세력추정매매", key: "screener" },
    { href: "/bollinger", label: "볼린저 밴드 반전 매매", key: "bollinger" },
    { href: "/friday", label: "금요일 마감 돌파", key: "friday" },
  ];

  /* 테마: 저장값(dark|light|"") → ?theme= 딥링크 → 없으면 시스템(prefers-color-scheme). */
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const urlTheme = new URLSearchParams(location.search).get("theme");
  let pref = "";                     // 사용자가 고른 값 ("" = 시스템)
  try { pref = localStorage.getItem("theme") || ""; } catch (e) {}
  if (pref !== "dark" && pref !== "light") pref = "";
  if (!pref && (urlTheme === "dark" || urlTheme === "light")) pref = urlTheme;

  function applyTheme() {
    const t = pref || (mq && mq.matches ? "dark" : "light");
    document.documentElement.setAttribute("data-bs-theme", t);
  }
  applyTheme();
  // 시스템 설정이 바뀌면(저장값이 없을 때) 따라간다.
  if (mq) {
    const onSys = () => {
      if (pref) return;
      applyTheme();
      document.dispatchEvent(new CustomEvent("themechange", { detail: "" }));
    };
    mq.addEventListener ? mq.addEventListener("change", onSys) : mq.addListener(onSys);
  }

  function toggleTheme() {
    pref = pref === "dark" ? "light" : pref === "light" ? "" : "dark";
    applyTheme();
    try { localStorage.setItem("theme", pref); } catch (e) {}
    document.dispatchEvent(new CustomEvent("themechange", { detail: pref }));
  }

  function render() {
    const host = document.querySelector("[data-gnb]");
    if (!host) return;
    const active = host.dataset.gnb;
    const nav = document.createElement("nav");
    nav.className = "navbar sticky-top bg-s1 border-bottom py-1";
    nav.innerHTML = `<div class="container-fluid page-wrap d-flex flex-wrap align-items-center gap-2">
      <a class="navbar-brand d-inline-flex align-items-center gap-2 me-2 fw-bolder fs-6" href="/">
        <span class="d-inline-flex align-items-center justify-content-center rounded-2 bg-accent text-white fw-bolder"
              style="width:22px;height:22px;font-size:12px">왔</span>왔니 주식</a>
      <ul class="nav gap-1 flex-wrap">${MENU.map(m =>
        `<li class="nav-item"><a class="nav-link px-3 py-1 rounded-2 text-nowrap${m.key === active ? " active tint-accent text-accent fw-bold" : " text-body-secondary"}" href="${m.href}"${m.key === active ? ' aria-current="page"' : ""}>${m.label}</a></li>`
      ).join("")}</ul>
      <div class="flex-grow-1"></div>
      <div class="d-flex align-items-center gap-2 flex-wrap" id="gnb-actions"></div>
    </div>`;
    host.replaceWith(nav);

    // 페이지가 미리 심어 둔 버튼들을 GNB 오른쪽으로 옮긴다.
    const slot = nav.querySelector("#gnb-actions");
    document.querySelectorAll("[data-gnb-action]").forEach(el => slot.appendChild(el));

    // 로그인 사용자 표시 + 로그아웃 (로그인 기능이 켜진 서버에서만 보인다)
    fetch("/api/me").then(r => r.json()).then(me => {
      if (!me.login_required || !me.username) return;
      const who = document.createElement("button");
      who.className = UI.cls.btnGhost;
      who.title = "내 계정 (비밀번호 변경 · 탈퇴)";
      who.textContent = (me.is_admin ? "★ " : "") + me.username;
      who.onclick = () => Account.open(me);
      const out = document.createElement("button");
      out.className = UI.cls.btn;
      out.id = "logout-btn";
      out.textContent = "로그아웃";
      out.onclick = async () => {
        await fetch("/api/logout", { method: "POST" });
        location.href = "/login";
      };
      slot.appendChild(who);
      slot.appendChild(out);
    }).catch(() => {});

    const themeBtn = document.createElement("button");
    themeBtn.className = UI.cls.btn;
    themeBtn.id = "theme-btn";
    themeBtn.title = "테마 전환 (라이트 / 다크 / 시스템)";
    themeBtn.textContent = "◐ 테마";
    themeBtn.onclick = toggleTheme;
    slot.appendChild(themeBtn);
  }

  /* 내 계정 창 — 비밀번호 변경 · 회원 탈퇴 */
  const Account = (() => {
    let back = null;
    // gnb.js 는 ui.js 보다 먼저 로드되므로 UI.cls 는 창을 열 때 읽는다.
    const buildHtml = () => { const c = UI.cls; return `<div class="modal fade" id="acct-modal" tabindex="-1" aria-labelledby="acct-title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <div>
          <h2 id="acct-title" class="modal-title fs-5 fw-semibold">내 계정</h2>
          <div class="small text-body-tertiary" id="acct-sub"></div>
        </div>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="닫기"></button>
      </div>
      <div class="modal-body">
        <fieldset class="${c.fieldset}">
          <legend class="${c.legend}">비밀번호 변경</legend>
          <div class="mb-2"><label class="${c.label}" for="acct-cur">현재 비밀번호</label>
            <input class="${c.input}" type="password" id="acct-cur" autocomplete="current-password"></div>
          <div class="row g-2">
            <div class="col-12 col-sm-6"><label class="${c.label}" for="acct-new">새 비밀번호</label>
              <input class="${c.input}" type="password" id="acct-new" autocomplete="new-password">
              <div class="${c.hint}">8자 이상. 바꾸면 다른 기기의 로그인은 끊깁니다.</div></div>
            <div class="col-12 col-sm-6"><label class="${c.label}" for="acct-new2">새 비밀번호 확인</label>
              <input class="${c.input}" type="password" id="acct-new2" autocomplete="new-password"></div>
          </div>
          <div class="d-flex align-items-center gap-2 flex-wrap mt-2 mb-1">
            <button class="${c.btnPrimary}" id="acct-pw">변경</button></div>
        </fieldset>
        <fieldset class="${c.fieldset}">
          <legend class="${c.legend}">회원 탈퇴</legend>
          <p class="small text-body-tertiary mb-2">저장된 API 키·텔레그램·볼린저 설정이 모두 지워지고 진행 중인 스캔·봇이 멈춥니다.</p>
          <div class="d-flex align-items-center gap-2 flex-wrap mb-1">
            <input class="${c.input}" style="width:auto" type="password" id="acct-del-pw" placeholder="비밀번호" autocomplete="current-password">
            <button class="${c.btnDanger}" id="acct-del">탈퇴</button>
          </div>
        </fieldset>
        <div class="small" id="acct-msg"></div>
      </div>
      <div class="modal-footer">
        <button class="${c.btn}" id="acct-close" data-bs-dismiss="modal">닫기</button>
      </div>
    </div>
  </div>
</div>`; };
    const q = (s) => document.querySelector(s);
    const TONE = { bad: "text-bad", ok: "text-good", "": "text-body-secondary" };
    const say = (t, k = "") => {
      const el = q("#acct-msg");
      el.className = `small ${TONE[k] || TONE[""]}`;
      el.textContent = t;
    };
    async function call(method, url, body) {
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out.detail || "요청 실패");
      return out;
    }
    function mount() {
      const h = document.createElement("div");
      h.innerHTML = buildHtml();
      back = h.firstElementChild;
      document.body.appendChild(back);
      q("#acct-pw").onclick = async () => {
        const cur = q("#acct-cur").value, nw = q("#acct-new").value;
        if (nw !== q("#acct-new2").value) return say("새 비밀번호 확인이 일치하지 않습니다.", "bad");
        try {
          const out = await call("POST", "/api/account/password", { current: cur, new: nw });
          ["#acct-cur", "#acct-new", "#acct-new2"].forEach(s => q(s).value = "");
          say(`비밀번호를 바꿨습니다.${out.revoked ? ` 다른 기기 로그인 ${out.revoked}건을 끊었습니다.` : ""}`, "ok");
        } catch (e) { say(e.message, "bad"); }
      };
      q("#acct-del").onclick = async () => {
        if (!confirm("정말 탈퇴할까요? 되돌릴 수 없습니다.")) return;
        try {
          await call("DELETE", "/api/account", { password: q("#acct-del-pw").value });
          location.href = "/login";
        } catch (e) { say(e.message, "bad"); }
      };
    }
    return {
      open(me) {
        if (!back) mount();
        q("#acct-sub").textContent = `${me.username} · ${me.is_admin
          ? "관리자 (서버 계좌 주문·봇 사용 가능)" : "일반 사용자 (본인 API 키로만 주문·봇 사용)"}`;
        say("");
        bootstrap.Modal.getOrCreateInstance(back).show();
      },
    };
  })();

  window.GNB = { toggleTheme };
  document.addEventListener("DOMContentLoaded", render);
})();
