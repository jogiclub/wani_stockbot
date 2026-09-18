/* 공통 GNB(전역 내비게이션) + 테마 토글.
   세 화면(수급포착매매·세력추정매매·볼린저 반전 매매)이 같은 마크업을 쓰도록 JS 로 한 번만 그린다.
   페이지는 <div class="gnb-actions"> 안에 자기 버튼을 넣어 두면 오른쪽에 합쳐진다. */
(function () {
  const MENU = [
    { href: "/supply", label: "수급포착매매", key: "supply" },
    { href: "/", label: "세력추정매매", key: "screener" },
    { href: "/bollinger", label: "볼린저 밴드 반전 매매", key: "bollinger" },
  ];

  /* 테마: 저장값 → ?theme= 딥링크 → 시스템 기본 순. 페인트 전에 적용해 깜빡임을 막는다. */
  const urlTheme = new URLSearchParams(location.search).get("theme");
  if (urlTheme === "dark" || urlTheme === "light") {
    document.documentElement.setAttribute("data-theme", urlTheme);
  } else {
    try {
      const saved = localStorage.getItem("theme");
      if (saved) document.documentElement.setAttribute("data-theme", saved);
    } catch (e) {}
  }

  function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
    next ? document.documentElement.setAttribute("data-theme", next)
         : document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("theme", next); } catch (e) {}
    document.dispatchEvent(new CustomEvent("themechange", { detail: next }));
  }

  function render() {
    const host = document.querySelector("[data-gnb]");
    if (!host) return;
    const active = host.dataset.gnb;
    const link = "px-3 py-1.5 rounded-lg no-underline text-txt2 font-medium whitespace-nowrap " +
                 "hover:bg-s2 hover:text-txt aria-[current=page]:tint-accent " +
                 "aria-[current=page]:text-accent aria-[current=page]:font-bold";
    const nav = document.createElement("nav");
    nav.className = "sticky top-0 z-50 bg-s1 border-b border-line";
    nav.innerHTML = `<div class="max-w-page mx-auto px-5 flex items-center gap-2 min-h-[54px] flex-wrap">
      <a class="inline-flex items-center gap-[7px] mr-2.5 text-[17px] font-extrabold tracking-tight
                text-txt no-underline whitespace-nowrap" href="/">
        <span class="w-[22px] h-[22px] rounded-[7px] bg-accent text-white text-xs font-extrabold
                     inline-flex items-center justify-center">왔</span>왔니 주식</a>
      <div class="flex gap-0.5 flex-wrap">${MENU.map(m =>
        `<a class="${link}" href="${m.href}"${m.key === active ? ' aria-current="page"' : ""}>${m.label}</a>`
      ).join("")}</div>
      <div class="flex-1"></div>
      <div class="flex items-center gap-2 flex-wrap" id="gnb-actions"></div>
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
    const c = UI.cls;
    const HTML = `<div class="${c.modalBack}" id="acct-modal" hidden role="dialog" aria-modal="true" aria-labelledby="acct-title">
  <div class="${c.modal}">
    <h2 id="acct-title" class="m-0 mb-1 text-[17px] font-semibold">내 계정</h2>
    <p class="m-0 mb-3.5 text-[12.5px] text-txt3" id="acct-sub"></p>
    <fieldset class="${c.fieldset}">
      <legend class="${c.legend}">비밀번호 변경</legend>
      <div class="mb-2.5"><label class="${c.label}" for="acct-cur">현재 비밀번호</label>
        <input class="${c.input}" type="password" id="acct-cur" autocomplete="current-password"></div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        <div><label class="${c.label}" for="acct-new">새 비밀번호</label>
          <input class="${c.input}" type="password" id="acct-new" autocomplete="new-password">
          <div class="${c.hint}">8자 이상. 바꾸면 다른 기기의 로그인은 끊깁니다.</div></div>
        <div><label class="${c.label}" for="acct-new2">새 비밀번호 확인</label>
          <input class="${c.input}" type="password" id="acct-new2" autocomplete="new-password"></div>
      </div>
      <div class="flex items-center gap-2.5 flex-wrap mt-2">
        <button class="${c.btnPrimary}" id="acct-pw">변경</button></div>
    </fieldset>
    <fieldset class="${c.fieldset}">
      <legend class="${c.legend}">회원 탈퇴</legend>
      <p class="m-0 mb-2 text-[12.5px] text-txt3">저장된 API 키·텔레그램·볼린저 설정이 모두 지워지고 진행 중인 스캔·봇이 멈춥니다.</p>
      <div class="flex items-center gap-2.5 flex-wrap">
        <input class="${c.input} !w-auto" type="password" id="acct-del-pw" placeholder="비밀번호" autocomplete="current-password">
        <button class="${c.btnDanger}" id="acct-del">탈퇴</button>
      </div>
    </fieldset>
    <div class="flex flex-wrap gap-2 items-center mt-3.5"><div class="flex-1"></div>
      <button class="${c.btn}" id="acct-close">닫기</button></div>
    <div class="text-[12.5px] mt-2.5" id="acct-msg"></div>
  </div>
</div>`;
    const q = (s) => document.querySelector(s);
    const TONE = { bad: "text-bad", ok: "text-good", "": "text-txt2" };
    const say = (t, k = "") => {
      const el = q("#acct-msg");
      el.className = `text-[12.5px] mt-2.5 ${TONE[k] || TONE[""]}`;
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
      h.innerHTML = HTML;
      back = h.firstElementChild;
      document.body.appendChild(back);
      const close = () => { back.hidden = true; };
      q("#acct-close").onclick = close;
      back.addEventListener("click", ev => { if (ev.target === back) close(); });
      document.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
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
        back.hidden = false;
      },
    };
  })();

  window.GNB = { toggleTheme };
  document.addEventListener("DOMContentLoaded", render);
})();
