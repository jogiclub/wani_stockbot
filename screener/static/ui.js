/* 공통 UI 기반 — Tailwind 설정 + AG Grid 헬퍼. 모든 화면이 가장 먼저 읽는다.

   읽는 순서 (페이지 <head>)
     1) https://cdn.tailwindcss.com          Tailwind Play CDN
     2) /static/ui.js                        (이 파일) tailwind.config + UI + Grid
     3) ag-grid-community.min.js             AG Grid (CDN)
     4) /static/common.css                   테마 토큰(CSS 변수)만 들어 있다

   색은 전부 CSS 변수(`common.css`)를 가리킨다 — 라이트/다크 전환이 클래스 교체 없이 된다.
   그래서 Tailwind 의 `dark:` 변형은 쓰지 않는다. `bg-s1 text-txt` 처럼 쓰면 테마를 따라간다.
   ⚠ CSS 변수 색이라 `bg-s1/50` 같은 **투명도 수식은 쓸 수 없다**. 필요하면 `bg-[var(--...)]`
   또는 `.tint-*` 유틸(common.css)을 쓴다. */
(function () {
  if (window.tailwind) {
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            s0: "var(--surface-0)", s1: "var(--surface-1)", s2: "var(--surface-2)",
            line: "var(--border)",
            txt: "var(--text-primary)", txt2: "var(--text-secondary)", txt3: "var(--text-muted)",
            up: "var(--up)", down: "var(--down)",
            good: "var(--good)", warn: "var(--warning)", bad: "var(--critical)",
            accent: "var(--accent)",
          },
          fontFamily: {
            sans: ['-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Apple SD Gothic Neo',
                   'Malgun Gothic', 'Noto Sans KR', 'sans-serif'],
          },
          maxWidth: { page: "1280px" },
        },
      },
    };
  }

  /* 화면·모듈이 함께 쓰는 클래스 묶음. 같은 모양을 여러 파일에 베껴 쓰지 않기 위한 것이다. */
  const cls = {
    btn: "font-sans text-sm rounded-lg border border-line bg-s1 text-txt px-3.5 py-1.5 " +
         "hover:bg-s2 disabled:opacity-50 disabled:cursor-not-allowed",
    btnPrimary: "font-sans text-sm rounded-lg border border-transparent bg-accent text-white " +
                "font-semibold px-3.5 py-1.5 hover:brightness-110 " +
                "disabled:opacity-50 disabled:cursor-not-allowed",
    btnDanger: "font-sans text-sm rounded-lg border border-transparent bg-bad text-white " +
               "font-semibold px-3.5 py-1.5 hover:brightness-110 " +
               "disabled:opacity-50 disabled:cursor-not-allowed",
    btnGhost: "font-sans text-xs rounded-lg border border-transparent bg-transparent text-txt2 " +
              "px-2 py-1 hover:border-line",
    card: "bg-s1 border border-line rounded-xl",
    input: "font-sans text-sm w-full px-2.5 py-2 rounded-lg border border-line bg-s0 text-txt " +
           "tabular-nums",
    label: "block text-xs text-txt2 mb-1",
    hint: "text-[11.5px] text-txt3 mt-1",
    legend: "text-xs text-txt3 px-1.5",
    fieldset: "border border-line rounded-[10px] px-3.5 py-3 mb-3",
    modalBack: "fixed inset-0 z-[60] bg-black/45 flex items-start justify-center " +
               "px-4 py-10 overflow-y-auto",
    modal: "bg-s1 border border-line rounded-2xl w-[min(560px,100%)] px-5 pt-5 pb-4 " +
           "shadow-[0_18px_50px_rgba(0,0,0,.3)]",
    tile: "bg-s1 border border-line rounded-xl px-4 py-3.5",
    tileLabel: "text-xs text-txt3 mb-1",
    tileValue: "text-[26px] font-semibold tracking-tight tabular-nums",
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
                       cellClass: "tabular-nums" },
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
  const upDown = (v) => (v > 0 ? "text-up font-semibold"
    : v < 0 ? "text-down font-semibold" : "text-txt3");

  window.UI = { cls, num, pct, signed, upDown };
  window.Grid = { create, LOCALE };
})();
