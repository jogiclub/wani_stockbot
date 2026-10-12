/* 공통 UI 기반 — Bootstrap 5.3 클래스 묶음 + AG Grid 헬퍼. 모든 화면이 읽는다.

   읽는 순서 (페이지 <head>)
     1) bootstrap.min.css (CDN) · pretendard (CDN)
     2) /static/common.css           테마 토큰(CSS 변수) → Bootstrap 변수 매핑 + 보조 클래스
     3) /static/gnb.js               <html data-bs-theme> 를 가장 먼저 정한다 (깜빡임 방지)
     4) ag-grid-community.min.js (CDN)
     5) /static/ui.js                (이 파일)
   <body> 끝: bootstrap.bundle.min.js (모달·오프캔버스·툴팁 JS) */
(function () {
  /* 화면·모듈이 함께 쓰는 Bootstrap 클래스 묶음. 같은 모양을 여러 파일에 베껴 쓰지 않기 위한 것이다. */
  const cls = {
    btn: "btn btn-sm btn-outline-secondary",
    btnPrimary: "btn btn-sm btn-primary fw-semibold",
    btnDanger: "btn btn-sm btn-danger fw-semibold",
    btnGhost: "btn btn-sm btn-link text-body-secondary text-decoration-none",
    card: "card",
    input: "form-control form-control-sm tnum",
    label: "form-label small text-body-secondary mb-1",
    hint: "form-text",
    legend: "small text-body-tertiary px-1 float-none w-auto fs-6",
    fieldset: "border rounded-3 px-3 py-2 mb-3",
    tile: "card card-body px-3 py-3",
    tileLabel: "small text-body-tertiary mb-1",
    tileValue: "fs-4 fw-semibold tnum",
  };

  /* ------------------------------------------------------------------ AG Grid */
  const LOCALE = {
    noRowsToShow: "표시할 항목이 없습니다",
    loadingOoo: "불러오는 중…",
    to: "~", of: "/", page: "쪽", next: "다음", last: "마지막", first: "처음", previous: "이전",
  };

  /** 그리드 1개를 만든다. `columns` 는 AG Grid colDef 배열 그대로.
   *  - 테마는 CSS 변수를 그대로 넘겨 라이트/다크를 따라가게 한다.
   *  - 행 클릭은 `onRow(data, event)` 로 받는다 (종목 메뉴 버튼 클릭은 페이지가 걸러낸다).
   *  반환값: AG Grid API (`api.setGridOption("rowData", …)`). */
  function create(el, { columns, rows = [], onRow = null, rowHeight = 40, options = {} } = {}) {
    if (!window.agGrid) return null;
    const theme = agGrid.themeQuartz.withParams({
      backgroundColor: "var(--surface-1)",
      foregroundColor: "var(--text-primary)",
      borderColor: "var(--border)",
      headerBackgroundColor: "var(--surface-2)",
      headerTextColor: "var(--text-muted)",
      rowHoverColor: "var(--surface-2)",
      selectedRowBackgroundColor: "var(--surface-2)",
      oddRowBackgroundColor: "var(--surface-1)",
      accentColor: "var(--accent)",
      wrapperBorderRadius: "12px",
      fontFamily: "inherit",
      fontSize: "13px",
      headerFontSize: "12px",
    });
    const api = agGrid.createGrid(el, {
      theme,
      localeText: LOCALE,
      rowData: rows,
      columnDefs: columns,
      rowHeight,
      headerHeight: 38,
      suppressCellFocus: true,
      defaultColDef: { sortable: true, resizable: true, suppressMovable: true,
                       cellClass: "tnum" },
      onRowClicked: onRow ? (ev) => onRow(ev.data, ev.event) : undefined,
      ...options,
    });
    return api;
  }

  /* 자주 쓰는 값 포맷 — 표와 상세 패널이 같은 규칙을 쓰도록 한곳에 둔다. */
  const num = (v, d = 0) => (v == null || v !== v ? "–"
    : Number(v).toLocaleString("ko-KR", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const pct = (v, d = 1) => (v == null || v !== v ? "–" : `${Number(v).toFixed(d)}%`);
  const signed = (v, d = 1) => (v == null || v !== v ? "–"
    : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%`);

  /** 증감 값 칸에 색을 입힌다 (상승=적색, 하락=청색 — 한국 시장 관례). */
  const upDown = (v) => (v > 0 ? "text-up fw-semibold"
    : v < 0 ? "text-down fw-semibold" : "text-body-tertiary");

  window.UI = { cls, num, pct, signed, upDown };
  window.Grid = { create, LOCALE };
})();
