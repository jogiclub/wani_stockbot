/* 공통 GNB(전역 내비게이션) + 테마 토글.
   두 화면(세력추정매매·볼린저 반전 매매)이 같은 마크업을 쓰도록 JS 로 한 번만 그린다.
   페이지는 <div class="gnb-actions"> 안에 자기 버튼을 넣어 두면 오른쪽에 합쳐진다. */
(function () {
  const MENU = [
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
    const nav = document.createElement("nav");
    nav.className = "gnb";
    nav.innerHTML = `<div class="gnb-inner">
      <a class="gnb-brand" href="/"><span class="mark">왔</span>왔니 주식</a>
      <div class="gnb-menu">${MENU.map(m =>
        `<a href="${m.href}"${m.key === active ? ' aria-current="page"' : ""}>${m.label}</a>`
      ).join("")}</div>
      <div class="gnb-spacer"></div>
      <div class="gnb-actions" id="gnb-actions"></div>
    </div>`;
    host.replaceWith(nav);

    // 페이지가 미리 심어 둔 버튼들을 GNB 오른쪽으로 옮긴다.
    const slot = nav.querySelector("#gnb-actions");
    document.querySelectorAll("[data-gnb-action]").forEach(el => slot.appendChild(el));

    const themeBtn = document.createElement("button");
    themeBtn.id = "theme-btn";
    themeBtn.title = "테마 전환 (라이트 / 다크 / 시스템)";
    themeBtn.textContent = "◐ 테마";
    themeBtn.onclick = toggleTheme;
    slot.appendChild(themeBtn);
  }

  window.GNB = { toggleTheme };
  document.addEventListener("DOMContentLoaded", render);
})();
