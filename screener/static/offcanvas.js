/* 공통 상세 offcanvas (Bootstrap Offcanvas) — 각 화면의 #detail 을 오른쪽(또는 아래)에서 밀려 나오는 패널로 감싼다.
   화면 코드는 그대로 #detail 에 innerHTML 을 쓰면 되고(비우거나 hidden 이면 닫힘),
   사용자가 닫으면 다음 그리드 행 클릭 전까지는 폴링 재렌더로 다시 열리지 않는다.
   #detail[data-oc="bottom"] 이면 아래에서 올라오는 패널, 아니면 오른쪽 패널.
   #detail 안의 [data-oc-head] 요소는 offcanvas 헤더로 복제해 올리고 원본은 숨긴다. */
(function () {
  function init() {
    const detail = document.getElementById("detail");
    if (!detail || !window.bootstrap) return;
    const bottom = detail.dataset.oc === "bottom";

    const panel = document.createElement("div");
    panel.id = "oc-panel";
    panel.className = "offcanvas " + (bottom ? "offcanvas-bottom h-auto" : "offcanvas-end");
    panel.tabIndex = -1;
    panel.setAttribute("aria-label", "상세");
    panel.style.cssText = bottom ? "max-height:85vh" : "--bs-offcanvas-width:min(720px,100vw)";
    panel.innerHTML = `<div class="offcanvas-header border-bottom bg-s1 py-2">
        <div id="oc-title" class="flex-grow-1 min-w-0"></div>
        <button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="닫기"></button>
      </div>
      <div class="offcanvas-body"><div id="oc-body"></div></div>`;
    document.body.appendChild(panel);
    const body = panel.querySelector("#oc-body");
    if (bottom) body.style.cssText = "max-width:1100px;width:100%;margin:0 auto";
    body.appendChild(detail);

    const oc = bootstrap.Offcanvas.getOrCreateInstance(panel, { backdrop: true, keyboard: true, scroll: false });
    const title = panel.querySelector("#oc-title");
    let dismissed = false;     // 사용자가 닫았는가 (다음 행 클릭 전까지 다시 열지 않는다)
    let programmatic = false;  // 코드가 닫는 중인가 (dismissed 로 치지 않는다)
    let shown = false;         // 열려 있거나 열리는 중

    const hasContent = () => !detail.hidden && detail.textContent.trim() !== "";
    // 화면이 data-oc-head 로 표시한 제목줄은 offcanvas 헤더로 옮긴다 (본문 쪽 원본은 숨김)
    const syncHead = () => {
      const h = detail.querySelector("[data-oc-head]");
      if (!h) { title.innerHTML = ""; return; }
      title.className = "flex-grow-1 min-w-0 " + h.className;
      title.innerHTML = h.innerHTML;
      h.style.display = "none";
    };
    const sync = () => {
      syncHead();
      const want = hasContent() && !dismissed;
      if (want && !shown) { shown = true; oc.show(); }
      else if (!want && shown) { shown = false; programmatic = true; oc.hide(); }
    };

    panel.addEventListener("hide.bs.offcanvas", () => {
      shown = false;
      if (!programmatic) dismissed = true;     // × · ESC · 배경 클릭
      programmatic = false;
    });
    // 전환 애니메이션 중에 들어온 변경은 Bootstrap 이 무시하므로 끝난 뒤 다시 맞춘다.
    panel.addEventListener("hidden.bs.offcanvas", sync);
    panel.addEventListener("shown.bs.offcanvas", sync);

    new MutationObserver(sync).observe(detail, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ["hidden"],
    });
    // 그리드 행 클릭 = 새로 열겠다는 뜻 (캡처 단계라 화면의 클릭 처리보다 먼저 풀린다)
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".ag-row")) {
        dismissed = false;
        setTimeout(sync, 0);
      }
    }, true);
    sync();
  }
  if (document.readyState === "complete") init();
  else window.addEventListener("load", init);   // bootstrap.bundle 은 body 끝에서 로드된다
})();
