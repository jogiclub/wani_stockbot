/* 공통 상세 offcanvas — 각 화면의 #detail 을 오른쪽에서 밀려 나오는 패널로 감싼다.
   화면 코드는 그대로 #detail 에 innerHTML 을 쓰면 되고(비우거나 hidden 이면 닫힘),
   사용자가 닫으면 다음 그리드 행 클릭 전까지는 폴링 재렌더로 다시 열리지 않는다. */
(function () {
  const css = document.createElement("style");
  css.textContent = `
    #oc-back { position: fixed; inset: 0; z-index: 55; background: rgba(0,0,0,.35);
      opacity: 0; pointer-events: none; transition: opacity .2s; }
    #oc-panel { position: fixed; top: 0; right: 0; bottom: 0; z-index: 56; width: min(720px, 100vw);
      background: var(--surface-0); border-left: 1px solid var(--border); box-shadow: -8px 0 24px rgba(0,0,0,.18);
      transform: translateX(100%); transition: transform .22s ease; display: flex; flex-direction: column; }
    #oc-panel.open { transform: none; }
    #oc-back.open { opacity: 1; pointer-events: auto; }
    #oc-head { display: flex; justify-content: flex-end; padding: 8px 12px; border-bottom: 1px solid var(--border);
      background: var(--surface-1); }
    #oc-close { border: 0; background: transparent; color: var(--text-secondary); font-size: 22px; line-height: 1; cursor: pointer; padding: 2px 8px; }
    #oc-close:hover { color: var(--text-primary); }
    #oc-body { flex: 1; overflow-y: auto; padding: 0 16px 24px; }
  `;
  document.head.appendChild(css);

  function init() {
    const detail = document.getElementById("detail");
    if (!detail) return;
    const back = document.createElement("div"); back.id = "oc-back";
    const panel = document.createElement("aside");
    panel.id = "oc-panel"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "상세");
    panel.innerHTML = `<div id="oc-head"><button id="oc-close" type="button" aria-label="닫기">×</button></div><div id="oc-body"></div>`;
    document.body.append(back, panel);
    panel.querySelector("#oc-body").appendChild(detail);

    let dismissed = false;
    const hasContent = () => !detail.hidden && detail.textContent.trim() !== "";
    const sync = () => {
      const open = hasContent() && !dismissed;
      panel.classList.toggle("open", open);
      back.classList.toggle("open", open);
    };
    const close = () => { dismissed = true; sync(); };

    new MutationObserver(sync).observe(detail, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["hidden"] });
    back.addEventListener("click", close);
    panel.querySelector("#oc-close").addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    // 그리드 행 클릭 = 새로 열겠다는 뜻 (캡처 단계라 화면의 클릭 처리보다 먼저 풀린다)
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".ag-row")) { dismissed = false; }
    }, true);
    sync();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
