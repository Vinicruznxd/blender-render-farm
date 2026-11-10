from __future__ import annotations

from textwrap import dedent


def build_dashboard_html() -> str:
    return dedent(
        """\
        <!doctype html>
        <html lang="pt-BR">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Coordinator Web Dashboard</title>
          <link rel="icon" type="image/png" href="/img/icon_coordinator.png">
          <style>
            :root {
              --bg: #06101b;
              --bg-soft: #0b1726;
              --panel: rgba(10, 23, 37, 0.86);
              --panel-strong: rgba(13, 29, 47, 0.96);
              --border: rgba(91, 138, 194, 0.24);
              --text: #edf4ff;
              --muted: #89a0bd;
              --accent: #3ea0ff;
              --accent-soft: rgba(62, 160, 255, 0.18);
              --orange: #ffab43;
              --green: #67d388;
              --green-soft: rgba(103, 211, 136, 0.16);
              --red: #ff7369;
              --red-soft: rgba(255, 115, 105, 0.16);
              --amber: #f3c05a;
              --amber-soft: rgba(243, 192, 90, 0.18);
              --cyan: #76c4ff;
              --shadow: 0 20px 60px rgba(0, 0, 0, 0.32);
              --radius: 20px;
            }

            * {
              box-sizing: border-box;
            }

            html, body {
              margin: 0;
              min-height: 100%;
              background:
                radial-gradient(circle at top left, rgba(45, 140, 255, 0.20), transparent 34%),
                radial-gradient(circle at top right, rgba(255, 171, 67, 0.12), transparent 26%),
                linear-gradient(180deg, #07121f 0%, #05101a 100%);
              color: var(--text);
              font-family: "Segoe UI", "Bahnschrift", sans-serif;
            }

            body {
              padding: 28px;
            }

            a {
              color: inherit;
              text-decoration: none;
            }

            button,
            a.button-link {
              border: 0;
              border-radius: 999px;
              padding: 10px 16px;
              font: inherit;
              color: var(--text);
              background: rgba(255, 255, 255, 0.06);
              cursor: pointer;
              transition: transform 0.16s ease, background 0.16s ease, border-color 0.16s ease;
              border: 1px solid rgba(255, 255, 255, 0.08);
            }

            button:hover,
            a.button-link:hover {
              transform: translateY(-1px);
              background: rgba(255, 255, 255, 0.11);
            }

            button.primary {
              background: linear-gradient(135deg, #2b8cff, #52b2ff);
              border-color: transparent;
              box-shadow: 0 10px 24px rgba(43, 140, 255, 0.26);
            }

            .page {
              max-width: 1680px;
              margin: 0 auto;
              display: grid;
              gap: 20px;
            }

            .hero {
              position: relative;
              overflow: hidden;
              display: grid;
              gap: 18px;
              padding: 26px;
              border-radius: 28px;
              background:
                linear-gradient(145deg, rgba(16, 34, 53, 0.94), rgba(8, 18, 30, 0.98)),
                linear-gradient(180deg, rgba(62, 160, 255, 0.08), transparent);
              border: 1px solid var(--border);
              box-shadow: var(--shadow);
            }

            .hero::after {
              content: "";
              position: absolute;
              inset: auto -120px -140px auto;
              width: 360px;
              height: 360px;
              border-radius: 50%;
              background: radial-gradient(circle, rgba(62, 160, 255, 0.18), transparent 70%);
              pointer-events: none;
            }

            .hero-top {
              display: flex;
              justify-content: space-between;
              align-items: flex-start;
              gap: 16px;
              flex-wrap: wrap;
            }

            .hero-title {
              display: grid;
              gap: 8px;
            }

            .eyebrow {
              letter-spacing: 0.18em;
              font-size: 12px;
              color: var(--orange);
              text-transform: uppercase;
            }

            h1 {
              margin: 0;
              font-size: clamp(30px, 5vw, 46px);
              line-height: 1.02;
              font-family: "Bahnschrift", "Segoe UI", sans-serif;
            }

            .subtitle {
              margin: 0;
              color: var(--muted);
              max-width: 900px;
            }

            .hero-credit,
            .hero-credit a {
              color: var(--cyan);
              font-size: 13px;
            }

            .hero-credit a:hover {
              color: var(--text);
            }

            .hero-actions {
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
            }

            .hero-meta {
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
            }

            .chip {
              display: inline-flex;
              align-items: center;
              gap: 8px;
              padding: 10px 12px;
              border-radius: 999px;
              background: rgba(255, 255, 255, 0.06);
              border: 1px solid rgba(255, 255, 255, 0.08);
              color: var(--muted);
              font-size: 13px;
            }

            .chip strong {
              color: var(--text);
              font-weight: 600;
            }

            .metrics-grid {
              display: grid;
              grid-template-columns: repeat(6, minmax(0, 1fr));
              gap: 14px;
            }

            .metric-card,
            .panel {
              border-radius: var(--radius);
              background: var(--panel);
              border: 1px solid var(--border);
              box-shadow: var(--shadow);
              backdrop-filter: blur(14px);
            }

            .metric-card {
              padding: 18px;
              min-height: 132px;
              position: relative;
              overflow: hidden;
            }

            .metric-card::after {
              content: "";
              position: absolute;
              inset: auto -14px -14px auto;
              width: 110px;
              height: 110px;
              border-radius: 50%;
              background: radial-gradient(circle, rgba(62, 160, 255, 0.16), transparent 70%);
            }

            .metric-label {
              text-transform: uppercase;
              letter-spacing: 0.12em;
              color: var(--muted);
              font-size: 12px;
              margin-bottom: 18px;
            }

            .metric-value {
              font-size: clamp(28px, 3vw, 38px);
              font-weight: 700;
              font-family: "Bahnschrift", "Segoe UI", sans-serif;
            }

            .metric-detail {
              margin-top: 12px;
              color: var(--muted);
              font-size: 13px;
            }

            .split-grid {
              display: grid;
              grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr);
              gap: 20px;
            }

            .panel {
              padding: 22px;
              overflow: hidden;
            }

            .panel-header {
              display: flex;
              justify-content: space-between;
              align-items: center;
              gap: 12px;
              margin-bottom: 18px;
              flex-wrap: wrap;
            }

            .panel-title {
              margin: 0;
              font-size: 20px;
              font-family: "Bahnschrift", "Segoe UI", sans-serif;
            }

            .panel-subtitle {
              color: var(--muted);
              font-size: 13px;
            }

            .status-pill {
              display: inline-flex;
              align-items: center;
              gap: 8px;
              padding: 6px 10px;
              border-radius: 999px;
              font-size: 12px;
              font-weight: 600;
              background: rgba(255, 255, 255, 0.08);
              color: var(--text);
              text-transform: uppercase;
              letter-spacing: 0.06em;
            }

            .status-running,
            .status-idle,
            .status-ready,
            .status-completed,
            .status-online {
              background: var(--green-soft);
              color: var(--green);
            }

            .status-rendering,
            .status-busy,
            .status-validating {
              background: var(--accent-soft);
              color: var(--cyan);
            }

            .status-blocked,
            .status-error,
            .status-offline,
            .status-cancelled {
              background: var(--red-soft);
              color: var(--red);
            }

            .status-queued,
            .status-paused,
            .status-unknown {
              background: var(--amber-soft);
              color: var(--amber);
            }

            .summary-grid {
              display: grid;
              grid-template-columns: repeat(2, minmax(0, 1fr));
              gap: 14px;
            }

            .summary-item {
              padding: 14px 16px;
              border-radius: 16px;
              background: var(--panel-strong);
              border: 1px solid rgba(255, 255, 255, 0.06);
            }

            .summary-label {
              color: var(--muted);
              font-size: 12px;
              text-transform: uppercase;
              letter-spacing: 0.12em;
              margin-bottom: 8px;
            }

            .summary-value {
              font-size: 15px;
              line-height: 1.45;
              word-break: break-word;
            }

            .problem-columns {
              display: grid;
              grid-template-columns: repeat(3, minmax(0, 1fr));
              gap: 14px;
            }

            .problem-box {
              padding: 16px;
              border-radius: 16px;
              background: var(--panel-strong);
              border: 1px solid rgba(255, 255, 255, 0.06);
            }

            .problem-box h3 {
              margin: 0 0 12px;
              font-size: 14px;
              text-transform: uppercase;
              letter-spacing: 0.12em;
              color: var(--muted);
            }

            .stack {
              display: grid;
              gap: 10px;
            }

            .list-card {
              padding: 12px 14px;
              border-radius: 14px;
              background: rgba(255, 255, 255, 0.04);
              border: 1px solid rgba(255, 255, 255, 0.05);
            }

            .list-title {
              font-weight: 600;
              margin-bottom: 4px;
            }

            .list-meta {
              color: var(--muted);
              font-size: 13px;
              line-height: 1.4;
              word-break: break-word;
            }

            table {
              width: 100%;
              border-collapse: collapse;
            }

            th, td {
              padding: 13px 12px;
              text-align: left;
              border-bottom: 1px solid rgba(255, 255, 255, 0.06);
              vertical-align: top;
            }

            th {
              color: var(--muted);
              font-size: 12px;
              text-transform: uppercase;
              letter-spacing: 0.1em;
              font-weight: 600;
            }

            tr {
              transition: background 0.16s ease;
            }

            tbody tr:hover {
              background: rgba(255, 255, 255, 0.04);
            }

            tbody tr.selected {
              background: rgba(62, 160, 255, 0.12);
            }

            .table-link {
              padding: 0;
              border: 0;
              background: transparent;
              color: var(--text);
              text-align: left;
              font-weight: 600;
            }

            .table-link:hover {
              color: var(--cyan);
              transform: none;
              background: transparent;
            }

            .mono {
              font-family: Consolas, "Courier New", monospace;
            }

            .muted {
              color: var(--muted);
            }

            .small {
              font-size: 12px;
            }

            .detail-grid {
              display: grid;
              gap: 14px;
            }

            .detail-block {
              padding: 16px;
              border-radius: 16px;
              background: var(--panel-strong);
              border: 1px solid rgba(255, 255, 255, 0.06);
            }

            .detail-block h3 {
              margin: 0 0 12px;
              font-size: 14px;
              text-transform: uppercase;
              letter-spacing: 0.12em;
              color: var(--muted);
            }

            details {
              border-radius: 16px;
              background: var(--panel-strong);
              border: 1px solid rgba(255, 255, 255, 0.06);
              padding: 12px 14px;
            }

            summary {
              cursor: pointer;
              font-weight: 600;
            }

            pre {
              margin: 14px 0 0;
              white-space: pre-wrap;
              word-break: break-word;
              font-family: Consolas, "Courier New", monospace;
              font-size: 12px;
              color: #cfe1fb;
            }

            .event-line {
              display: grid;
              gap: 4px;
              padding: 14px 0;
              border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            }

            .event-head {
              display: flex;
              gap: 10px;
              align-items: center;
              flex-wrap: wrap;
            }

            .event-body {
              color: var(--muted);
              line-height: 1.45;
            }

            .empty {
              padding: 22px;
              border-radius: 16px;
              background: rgba(255, 255, 255, 0.03);
              border: 1px dashed rgba(255, 255, 255, 0.08);
              color: var(--muted);
            }

            .two-up {
              display: grid;
              grid-template-columns: repeat(2, minmax(0, 1fr));
              gap: 20px;
            }

            .footer-note {
              color: var(--muted);
              font-size: 12px;
              text-align: right;
            }

            @media (max-width: 1320px) {
              .metrics-grid {
                grid-template-columns: repeat(3, minmax(0, 1fr));
              }

              .split-grid,
              .two-up,
              .problem-columns {
                grid-template-columns: 1fr;
              }
            }

            @media (max-width: 860px) {
              body {
                padding: 16px;
              }

              .hero,
              .panel {
                padding: 18px;
              }

              .metrics-grid,
              .summary-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
              }
            }

            @media (max-width: 620px) {
              .metrics-grid,
              .summary-grid {
                grid-template-columns: 1fr;
              }

              th:nth-child(5),
              td:nth-child(5),
              th:nth-child(6),
              td:nth-child(6),
              th:nth-child(7),
              td:nth-child(7) {
                display: none;
              }
            }
          </style>
        </head>
        <body>
          <div class="page">
            <section class="hero">
              <div class="hero-top">
                <div class="hero-title">
                  <div class="eyebrow">Blender Render Farm</div>
                  <h1>Coordinator Web Dashboard</h1>
                  <p class="subtitle">
                    Visao completa da fila, dos jobs, das maquinas e dos problemas operacionais da farm,
                    atualizada direto pela API local do coordinator.
                  </p>
                  <div class="hero-credit">
                    <a href="https://novaxd.tech/" target="_blank" rel="noreferrer">By Vinicius D'Arcadia Cruz - https://novaxd.tech/</a>
                  </div>
                </div>
                <div class="hero-actions">
                  <button id="refreshButton" class="primary" type="button">Atualizar agora</button>
                  <button id="toggleAutoRefreshButton" type="button">Auto refresh: ligado</button>
                  <a class="button-link" href="/docs" target="_blank" rel="noreferrer">API Docs</a>
                  <a class="button-link" href="/state" target="_blank" rel="noreferrer">JSON State</a>
                </div>
              </div>
              <div class="hero-meta" id="heroMeta"></div>
            </section>

            <section class="metrics-grid" id="metricsGrid"></section>

            <section class="split-grid">
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Resumo ativo</h2>
                    <div class="panel-subtitle">Coordinator, job ativo e parametros principais.</div>
                  </div>
                </div>
                <div id="activeJobPanel"></div>
              </article>
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Problemas e alertas</h2>
                    <div class="panel-subtitle">Pontos que merecem atencao agora.</div>
                  </div>
                </div>
                <div id="problemsPanel"></div>
              </article>
            </section>

            <section class="two-up">
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Relatorio por job</h2>
                    <div class="panel-subtitle">Clique em um job para abrir os detalhes completos.</div>
                  </div>
                  <div class="footer-note" id="jobsSummary"></div>
                </div>
                <div id="jobsTable"></div>
              </article>
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Detalhe do job</h2>
                    <div class="panel-subtitle">Parametros, progresso, frames e historico por maquina.</div>
                  </div>
                </div>
                <div id="jobDetailPanel"></div>
              </article>
            </section>

            <section class="two-up">
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Desempenho por maquina</h2>
                    <div class="panel-subtitle">Status, frame atual, GPU, Blender e falhas recentes.</div>
                  </div>
                  <div class="footer-note" id="workersSummary"></div>
                </div>
                <div id="workersTable"></div>
              </article>
              <article class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Detalhe da maquina</h2>
                    <div class="panel-subtitle">Capacidades, ultimo job reportado e dados brutos.</div>
                  </div>
                </div>
                <div id="workerDetailPanel"></div>
              </article>
            </section>

            <section class="panel">
              <div class="panel-header">
                <div>
                  <h2 class="panel-title">Eventos recentes</h2>
                  <div class="panel-subtitle">Logs estruturados para acompanhar falhas, dispatch e mudancas de estado.</div>
                </div>
                <div class="footer-note" id="lastUpdated"></div>
              </div>
              <div id="eventsPanel"></div>
            </section>
          </div>

          <script>
            const appState = {
              snapshot: null,
              logs: null,
              selectedJobId: null,
              selectedWorkerId: null,
              selectedJobDetail: null,
              jobRequestToken: 0,
              autoRefreshEnabled: true,
            };

            function escapeHtml(value) {
              const text = value === null || value === undefined ? "" : String(value);
              return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#39;");
            }

            function safeText(value, fallback = "-") {
              if (value === null || value === undefined) {
                return fallback;
              }
              const text = String(value).trim();
              return text ? text : fallback;
            }

            function truncate(value, maxLength = 96) {
              const text = safeText(value, "");
              if (!text) {
                return "-";
              }
              return text.length > maxLength ? text.slice(0, maxLength - 1) + "..." : text;
            }

            function toStatusClass(value) {
              const normalized = safeText(value, "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-");
              return "status-pill status-" + normalized;
            }

            function renderStatus(value) {
              const label = safeText(value).replace(/_/g, " ");
              return `<span class="${toStatusClass(value)}">${escapeHtml(label)}</span>`;
            }

            function formatPercent(value) {
              const numeric = Number(value);
              if (!Number.isFinite(numeric)) {
                return "-";
              }
              return numeric.toFixed(numeric % 1 === 0 ? 0 : 1) + "%";
            }

            function formatDate(value) {
              if (!value) {
                return "-";
              }
              const parsed = new Date(value);
              if (Number.isNaN(parsed.getTime())) {
                return safeText(value);
              }
              return parsed.toLocaleString("pt-BR");
            }

            function formatDuration(seconds) {
              const numeric = Number(seconds);
              if (!Number.isFinite(numeric) || numeric < 0) {
                return "-";
              }
              const totalSeconds = Math.round(numeric);
              const hours = Math.floor(totalSeconds / 3600);
              const minutes = Math.floor((totalSeconds % 3600) / 60);
              const remainingSeconds = totalSeconds % 60;
              if (hours > 0) {
                return `${hours}h ${minutes}m`;
              }
              if (minutes > 0) {
                return `${minutes}m ${remainingSeconds}s`;
              }
              return `${remainingSeconds}s`;
            }

            function updateAutoRefreshButton() {
              const button = document.getElementById("toggleAutoRefreshButton");
              button.textContent = appState.autoRefreshEnabled ? "Auto refresh: ligado" : "Auto refresh: pausado";
            }

            function captureWindowScroll() {
              return {
                x: window.scrollX || 0,
                y: window.scrollY || 0,
              };
            }

            function restoreWindowScroll(position) {
              if (!position) {
                return;
              }
              window.scrollTo(position.x || 0, position.y || 0);
            }

            function metricCard(label, value, detail) {
              return `
                <article class="metric-card">
                  <div class="metric-label">${escapeHtml(label)}</div>
                  <div class="metric-value">${escapeHtml(value)}</div>
                  <div class="metric-detail">${escapeHtml(detail)}</div>
                </article>
              `;
            }

            function emptyState(message) {
              return `<div class="empty">${escapeHtml(message)}</div>`;
            }

            function renderHero(snapshot) {
              const coordinator = snapshot?.coordinator || {};
              const discovery = coordinator.discovery || {};
              const metrics = snapshot?.metrics || {};
              const activeJob = snapshot?.activeJob || null;
              const primaryUrl = Array.isArray(discovery.apiUrls) && discovery.apiUrls.length > 0 ? discovery.apiUrls[0] : window.location.origin;
              const meta = [
                `<div class="chip"><span>Coordinator</span><strong>${escapeHtml(safeText(coordinator.coordinatorId))}</strong></div>`,
                `<div class="chip"><span>Status</span><strong>${escapeHtml(safeText(coordinator.status, "running"))}</strong></div>`,
                `<div class="chip"><span>API</span><strong>${escapeHtml(primaryUrl)}</strong></div>`,
                `<div class="chip"><span>Uptime do job</span><strong>${escapeHtml(formatDuration(metrics.elapsedSeconds))}</strong></div>`,
                `<div class="chip"><span>Job ativo</span><strong>${escapeHtml(activeJob ? safeText(activeJob.jobName) : "Nenhum")}</strong></div>`,
              ];
              document.getElementById("heroMeta").innerHTML = meta.join("");
            }

            function renderMetrics(snapshot) {
              const metrics = snapshot?.metrics || {};
              const masters = Array.isArray(snapshot?.masters) ? snapshot.masters.length : 0;
              const workers = Array.isArray(snapshot?.workers) ? snapshot.workers : [];
              const activeJob = snapshot?.activeJob || null;
              const jobs = Array.isArray(snapshot?.jobs) ? snapshot.jobs : [];
              const grid = document.getElementById("metricsGrid");
              grid.innerHTML = [
                metricCard("Masters online", String(masters), "Interfaces conectadas"),
                metricCard("Workers online", String(metrics.machinesOnline || 0), "Maquinas ativas"),
                metricCard("Rendering", String(metrics.machinesRendering || 0), "Maquinas ocupadas"),
                metricCard("Jobs totais", String(jobs.length), "Fila completa"),
                metricCard("Fila aguardando", String(metrics.totalQueuedJobs || 0), "Jobs queued"),
                metricCard("Job ativo", activeJob ? formatPercent(metrics.overallPercent || activeJob.progressPercent || 0) : "-", activeJob ? safeText(activeJob.jobName) : "Nenhum job em execucao"),
              ].join("");
            }

            function renderActiveJob(snapshot) {
              const coordinator = snapshot?.coordinator || {};
              const activeJob = snapshot?.activeJob || null;
              const metrics = snapshot?.metrics || {};
              const target = document.getElementById("activeJobPanel");

              if (!activeJob) {
                target.innerHTML = emptyState("Nenhum job ativo no momento. O coordinator segue monitorando a fila.");
                return;
              }

              target.innerHTML = `
                <div class="detail-grid">
                  <div class="detail-block">
                    <div class="panel-header">
                      <div>
                        <div class="panel-subtitle">Job em foco</div>
                        <h3 class="panel-title">${escapeHtml(safeText(activeJob.jobName))}</h3>
                      </div>
                      ${renderStatus(activeJob.status)}
                    </div>
                    <div class="summary-grid">
                      <div class="summary-item">
                        <div class="summary-label">Progresso</div>
                        <div class="summary-value">${escapeHtml(formatPercent(activeJob.progressPercent))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Frames</div>
                        <div class="summary-value">${escapeHtml(`${activeJob.completedFramesCount || 0}/${activeJob.totalFrames || 0}`)}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Blend file</div>
                        <div class="summary-value mono">${escapeHtml(safeText(activeJob.blendFile))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Output folder</div>
                        <div class="summary-value mono">${escapeHtml(safeText(activeJob.outputFolder))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Faixa de frames</div>
                        <div class="summary-value">${escapeHtml(`${activeJob.startFrame}-${activeJob.endFrame}`)}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">ETA estimado</div>
                        <div class="summary-value">${escapeHtml(formatDuration(metrics.etaSeconds))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Workers</div>
                        <div class="summary-value">${escapeHtml(activeJob.gpuOnlyWorkers ? "GPU only" : "Todos habilitados")}</div>
                      </div>
                    </div>
                  </div>
                  <div class="detail-block">
                    <h3>Coordinator</h3>
                    <div class="summary-grid">
                      <div class="summary-item">
                        <div class="summary-label">Coordinator ID</div>
                        <div class="summary-value">${escapeHtml(safeText(coordinator.coordinatorId))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Iniciado em</div>
                        <div class="summary-value">${escapeHtml(formatDate(coordinator.startedAt))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Versao app</div>
                        <div class="summary-value">${escapeHtml(safeText(coordinator.appVersion))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Protocol version</div>
                        <div class="summary-value">${escapeHtml(safeText(coordinator.protocolVersion))}</div>
                      </div>
                    </div>
                  </div>
                </div>
              `;
            }

            function renderProblems(snapshot, logs) {
              const jobs = Array.isArray(snapshot?.jobs) ? snapshot.jobs : [];
              const workers = Array.isArray(snapshot?.workers) ? snapshot.workers : [];
              const events = Array.isArray(logs?.events) ? logs.events : [];
              const blockedJobs = jobs.filter((job) => {
                const status = safeText(job.status, "").toLowerCase();
                return status === "blocked" || status === "error" || Number(job?.frameStats?.error || 0) > 0 || !!job.lastError;
              });
              const troubledWorkers = workers.filter((worker) => {
                const status = safeText(worker.status, "").toLowerCase();
                return status === "offline" || status === "error" || !!worker.error;
              });
              const recentWarnings = events.filter((event) => {
                const level = safeText(event.level, "").toLowerCase();
                return level === "warning" || level === "error";
              }).slice(0, 6);

              const blockedHtml = blockedJobs.length
                ? `<div class="stack">${blockedJobs.map((job) => `
                    <div class="list-card">
                      <div class="list-title">${escapeHtml(safeText(job.jobName))}</div>
                      <div class="list-meta">${escapeHtml(safeText(job.status))} | ${escapeHtml(formatPercent(job.progressPercent || 0))}</div>
                      <div class="list-meta">${escapeHtml(truncate(job.lastError || "Sem detalhe registrado.", 140))}</div>
                    </div>
                  `).join("")}</div>`
                : emptyState("Nenhum job com bloqueio ou erro no momento.");

              const workerHtml = troubledWorkers.length
                ? `<div class="stack">${troubledWorkers.map((worker) => `
                    <div class="list-card">
                      <div class="list-title">${escapeHtml(safeText(worker.name || worker.workerId))}</div>
                      <div class="list-meta">${escapeHtml(safeText(worker.status))} | ${escapeHtml(safeText(worker.gpu))}</div>
                      <div class="list-meta">${escapeHtml(truncate(worker.error || "Sem erro detalhado.", 140))}</div>
                    </div>
                  `).join("")}</div>`
                : emptyState("Nenhuma maquina offline ou em erro agora.");

              const warningHtml = recentWarnings.length
                ? `<div class="stack">${recentWarnings.map((event) => `
                    <div class="list-card">
                      <div class="list-title">${escapeHtml(safeText(event.message))}</div>
                      <div class="list-meta">${escapeHtml(safeText(event.category, "geral"))} | ${escapeHtml(formatDate(event.createdAt))}</div>
                    </div>
                  `).join("")}</div>`
                : emptyState("Sem warnings ou errors recentes nos eventos estruturados.");

              document.getElementById("problemsPanel").innerHTML = `
                <div class="problem-columns">
                  <div class="problem-box">
                    <h3>Jobs criticos</h3>
                    ${blockedHtml}
                  </div>
                  <div class="problem-box">
                    <h3>Maquinas com problema</h3>
                    ${workerHtml}
                  </div>
                  <div class="problem-box">
                    <h3>Eventos recentes</h3>
                    ${warningHtml}
                  </div>
                </div>
              `;
            }

            function renderJobs(snapshot) {
              const jobs = Array.isArray(snapshot?.jobs) ? snapshot.jobs : [];
              document.getElementById("jobsSummary").textContent = `${jobs.length} job(s)`;
              const container = document.getElementById("jobsTable");

              if (!jobs.length) {
                container.innerHTML = emptyState("Nenhum job foi enviado para a fila ainda.");
                return;
              }

              container.innerHTML = `
                <table>
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>Status</th>
                      <th>Progresso</th>
                      <th>Frames</th>
                      <th>Versao</th>
                      <th>Problema</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${jobs.map((job) => {
                      const version = job.requiredBlenderVersion || job.blendVersionDetected || "-";
                      const isSelected = appState.selectedJobId === job.jobId;
                      return `
                        <tr class="${isSelected ? "selected" : ""}" data-row-job-id="${escapeHtml(job.jobId)}">
                          <td>
                            <button class="table-link" type="button" data-job-id="${escapeHtml(job.jobId)}">${escapeHtml(safeText(job.jobName))}</button>
                            <div class="muted small mono">${escapeHtml(job.jobId)}</div>
                          </td>
                          <td>${renderStatus(job.status)}</td>
                          <td>${escapeHtml(formatPercent(job.progressPercent || 0))}</td>
                          <td>${escapeHtml(`${job.completedFrames || job.completedFramesCount || 0}/${job.totalFrames || 0}`)}</td>
                          <td>${escapeHtml(safeText(version))}</td>
                          <td class="small">${escapeHtml(truncate(job.lastError || "-", 92))}</td>
                        </tr>
                      `;
                    }).join("")}
                  </tbody>
                </table>
              `;

              container.querySelectorAll("[data-job-id]").forEach((element) => {
                element.addEventListener("click", () => {
                  loadJobDetail(element.getAttribute("data-job-id"));
                });
              });
            }

            function renderWorkers(snapshot) {
              const workers = Array.isArray(snapshot?.workers) ? snapshot.workers : [];
              document.getElementById("workersSummary").textContent = `${workers.length} maquina(s) visiveis`;
              const container = document.getElementById("workersTable");

              if (!workers.length) {
                container.innerHTML = emptyState("Nenhum worker visivel no state atual.");
                return;
              }

              container.innerHTML = `
                <table>
                  <thead>
                    <tr>
                      <th>Maquina</th>
                      <th>Status</th>
                      <th>Job atual</th>
                      <th>Frame</th>
                      <th>Progresso</th>
                      <th>Blender / GPU</th>
                      <th>Erro</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${workers.map((worker) => {
                      const isSelected = appState.selectedWorkerId === worker.workerId;
                      const blenderGpu = `${safeText(worker.blenderVersion)} | ${safeText(worker.gpu)}`;
                      return `
                        <tr class="${isSelected ? "selected" : ""}" data-row-worker-id="${escapeHtml(worker.workerId)}">
                          <td>
                            <button class="table-link" type="button" data-worker-id="${escapeHtml(worker.workerId)}">${escapeHtml(safeText(worker.name || worker.workerId))}</button>
                            <div class="muted small mono">${escapeHtml(worker.workerId)}</div>
                          </td>
                          <td>${renderStatus(worker.status)}</td>
                          <td class="small">${escapeHtml(safeText(worker.currentJobId))}</td>
                          <td>${escapeHtml(safeText(worker.currentFrame))}</td>
                          <td>${escapeHtml(formatPercent(worker.progress || 0))}</td>
                          <td class="small">${escapeHtml(truncate(blenderGpu, 92))}</td>
                          <td class="small">${escapeHtml(truncate(worker.error || "-", 92))}</td>
                        </tr>
                      `;
                    }).join("")}
                  </tbody>
                </table>
              `;

              container.querySelectorAll("[data-worker-id]").forEach((element) => {
                element.addEventListener("click", () => {
                  selectWorker(element.getAttribute("data-worker-id"));
                });
              });
            }

            async function loadJobDetail(jobId, options = {}) {
              const showLoading = options.showLoading !== false;
              if (!jobId) {
                appState.selectedJobId = null;
                appState.selectedJobDetail = null;
                renderJobDetail(null);
                renderJobs(appState.snapshot);
                return;
              }
              appState.selectedJobId = jobId;
              renderJobs(appState.snapshot);
              const requestToken = ++appState.jobRequestToken;
              const panel = document.getElementById("jobDetailPanel");
              if (showLoading && !appState.selectedJobDetail) {
                panel.innerHTML = emptyState("Carregando detalhes do job selecionado...");
              }
              try {
                const response = await fetch(`/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
                if (!response.ok) {
                  throw new Error(`Falha ao carregar job ${jobId}: HTTP ${response.status}`);
                }
                const detail = await response.json();
                if (requestToken !== appState.jobRequestToken) {
                  return;
                }
                appState.selectedJobDetail = detail;
                renderJobDetail(detail);
              } catch (error) {
                if (requestToken !== appState.jobRequestToken) {
                  return;
                }
                panel.innerHTML = emptyState(error instanceof Error ? error.message : "Falha ao carregar o job.");
              }
            }

            function renderJobDetail(detail) {
              const panel = document.getElementById("jobDetailPanel");
              if (!detail) {
                panel.innerHTML = emptyState("Selecione um job para ver os parametros completos e o relatorio detalhado.");
                return;
              }

              const frameStats = detail.frameStats || {};
              const workerStats = Array.isArray(detail.workerStats) ? detail.workerStats : [];
              const frames = Array.isArray(detail.frames) ? detail.frames : [];
              const problemFrames = frames.filter((frame) => {
                const status = safeText(frame.status, "").toLowerCase();
                return (
                  status === "leased" ||
                  status === "rendering" ||
                  status === "error" ||
                  Number(frame.attemptCount || 0) > 1 ||
                  !!frame.errorMessage ||
                  !!frame.lastFailureReason
                );
              }).sort((left, right) => {
                const leftErrored = safeText(left.status, "").toLowerCase() === "error" ? 0 : 1;
                const rightErrored = safeText(right.status, "").toLowerCase() === "error" ? 0 : 1;
                if (leftErrored !== rightErrored) {
                  return leftErrored - rightErrored;
                }
                const attemptDiff = Number(right.attemptCount || 0) - Number(left.attemptCount || 0);
                if (attemptDiff !== 0) {
                  return attemptDiff;
                }
                return Number(left.frameNumber || 0) - Number(right.frameNumber || 0);
              }).slice(0, 40);

              const workerStatsHtml = workerStats.length
                ? `
                    <table>
                      <thead>
                        <tr>
                          <th>Worker</th>
                          <th>Device</th>
                          <th>Success</th>
                          <th>Failure</th>
                          <th>Falhas seguidas</th>
                          <th>Ultimo erro</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${workerStats.map((item) => `
                          <tr>
                            <td>${escapeHtml(safeText(item.workerName || item.workerId))}<div class="muted small mono">${escapeHtml(safeText(item.workerId))}</div></td>
                            <td>${escapeHtml(safeText(item.renderDevicePreference, "AUTO"))}</td>
                            <td>${escapeHtml(String(item.successCount || 0))}</td>
                            <td>${escapeHtml(String(item.failureCount || 0))}</td>
                            <td>${escapeHtml(String(item.consecutiveFailureCount || 0))}</td>
                            <td class="small">${escapeHtml(truncate(item.lastFailureReason || "-", 96))}</td>
                          </tr>
                        `).join("")}
                      </tbody>
                    </table>
                  `
                : emptyState("Ainda nao existe historico consolidado por worker para este job.");

              const frameIssuesHtml = problemFrames.length
                ? `
                    <table>
                      <thead>
                        <tr>
                          <th>Frame</th>
                          <th>Status</th>
                          <th>Worker atual</th>
                          <th>Ultima falha</th>
                          <th>Tentativas</th>
                          <th>Erro / motivo</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${problemFrames.map((frame) => `
                          <tr>
                            <td>${escapeHtml(String(frame.frameNumber || "-"))}</td>
                            <td>${renderStatus(frame.status)}</td>
                            <td>${escapeHtml(safeText(frame.workerName || frame.workerId))}</td>
                            <td>${escapeHtml(safeText(frame.lastFailedWorkerName || frame.lastFailedWorkerId))}</td>
                            <td>${escapeHtml(String(frame.attemptCount || 0))}</td>
                            <td class="small">${escapeHtml(truncate(frame.errorMessage || frame.lastFailureReason || "-", 110))}</td>
                          </tr>
                        `).join("")}
                      </tbody>
                    </table>
                  `
                : emptyState("Nenhum frame com retry, render em andamento ou erro neste job.");

              panel.innerHTML = `
                <div class="detail-grid">
                  <div class="detail-block">
                    <div class="panel-header">
                      <div>
                        <div class="panel-subtitle mono">${escapeHtml(detail.jobId)}</div>
                        <h3 class="panel-title">${escapeHtml(safeText(detail.jobName))}</h3>
                      </div>
                      <div class="hero-actions">
                        ${renderStatus(detail.status)}
                        <a class="button-link" href="/jobs/${encodeURIComponent(detail.jobId)}/report.pdf" target="_blank" rel="noreferrer">Baixar PDF</a>
                      </div>
                    </div>
                    <div class="summary-grid">
                      <div class="summary-item">
                        <div class="summary-label">Blend file</div>
                        <div class="summary-value mono">${escapeHtml(safeText(detail.blendFile))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Output folder</div>
                        <div class="summary-value mono">${escapeHtml(safeText(detail.outputFolder))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Frames</div>
                        <div class="summary-value">${escapeHtml(`${detail.startFrame}-${detail.endFrame} (${detail.totalFrames || 0})`)}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Progresso</div>
                        <div class="summary-value">${escapeHtml(formatPercent(detail.progressPercent || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Versao Blender</div>
                        <div class="summary-value">${escapeHtml(safeText(detail.requiredBlenderVersion || detail.blendVersionDetected))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Politica</div>
                        <div class="summary-value">${escapeHtml(safeText(detail.versionRoutingPolicy))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Workers</div>
                        <div class="summary-value">${escapeHtml(detail.gpuOnlyWorkers ? "GPU only" : "Todos habilitados")}</div>
                      </div>
                    </div>
                  </div>
                  <div class="detail-block">
                    <h3>Resumo operacional</h3>
                    <div class="summary-grid">
                      <div class="summary-item">
                        <div class="summary-label">Done</div>
                        <div class="summary-value">${escapeHtml(String(frameStats.done || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Pending</div>
                        <div class="summary-value">${escapeHtml(String(frameStats.pending || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Leased</div>
                        <div class="summary-value">${escapeHtml(String(frameStats.leased || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Rendering</div>
                        <div class="summary-value">${escapeHtml(String(frameStats.rendering || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Error</div>
                        <div class="summary-value">${escapeHtml(String(frameStats.error || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Ultimo problema</div>
                        <div class="summary-value">${escapeHtml(truncate(detail.lastError || "Nenhum erro registrado.", 140))}</div>
                      </div>
                    </div>
                  </div>
                  <div class="detail-block">
                    <h3>Historico por maquina neste job</h3>
                    ${workerStatsHtml}
                  </div>
                  <div class="detail-block">
                    <h3>Frames com retries, problema ou render em andamento</h3>
                    ${frameIssuesHtml}
                  </div>
                  <details>
                    <summary>Parametros completos em JSON</summary>
                    <pre>${escapeHtml(JSON.stringify(detail, null, 2))}</pre>
                  </details>
                </div>
              `;
            }

            function selectWorker(workerId) {
              appState.selectedWorkerId = workerId;
              renderWorkers(appState.snapshot);
              renderWorkerDetail();
            }

            function renderWorkerDetail() {
              const panel = document.getElementById("workerDetailPanel");
              const workers = Array.isArray(appState.snapshot?.workers) ? appState.snapshot.workers : [];
              const worker = workers.find((item) => item.workerId === appState.selectedWorkerId);

              if (!worker) {
                panel.innerHTML = emptyState("Selecione uma maquina para ver status, capacidades e ultimo job reportado.");
                return;
              }

              const lastJob = worker.lastJob || null;
              const capabilities = worker.capabilities || {};
              const recoveryActions = Array.isArray(lastJob?.recoveryActions) ? lastJob.recoveryActions : [];
              const coordinatorReport = lastJob?.coordinatorReport || null;
              const persistentSessionAlert = lastJob && (
                lastJob.persistentSessionTemporarilyDisabled ||
                lastJob.persistentSessionDisabledReason ||
                lastJob.persistentSessionRetryAt
              );
              panel.innerHTML = `
                <div class="detail-grid">
                  <div class="detail-block">
                    <div class="panel-header">
                      <div>
                        <div class="panel-subtitle mono">${escapeHtml(worker.workerId)}</div>
                        <h3 class="panel-title">${escapeHtml(safeText(worker.name || worker.workerId))}</h3>
                      </div>
                      ${renderStatus(worker.status)}
                    </div>
                    <div class="summary-grid">
                      <div class="summary-item">
                        <div class="summary-label">IP / porta</div>
                        <div class="summary-value mono">${escapeHtml(`${safeText(worker.ip)}:${safeText(worker.port)}`)}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Frame atual</div>
                        <div class="summary-value">${escapeHtml(safeText(worker.currentFrame))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Job atual</div>
                        <div class="summary-value mono">${escapeHtml(safeText(worker.currentJobId))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Progresso</div>
                        <div class="summary-value">${escapeHtml(formatPercent(worker.progress || 0))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Blender</div>
                        <div class="summary-value">${escapeHtml(safeText(worker.blenderVersion))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">GPU / device</div>
                        <div class="summary-value">${escapeHtml(`${safeText(worker.gpu)} | ${safeText(worker.renderDevicePreference)}`)}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Ultimo seen</div>
                        <div class="summary-value">${escapeHtml(formatDate(worker.lastSeen))}</div>
                      </div>
                      <div class="summary-item">
                        <div class="summary-label">Erro atual</div>
                        <div class="summary-value">${escapeHtml(truncate(worker.error || "Nenhum erro atual.", 140))}</div>
                      </div>
                    </div>
                  </div>
                  <div class="detail-block">
                    <h3>Diagnostico do ultimo render</h3>
                    ${lastJob ? `
                      <div class="summary-grid">
                        <div class="summary-item">
                          <div class="summary-label">Resultado</div>
                          <div class="summary-value">${renderStatus(lastJob.result || "unknown")}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Modo</div>
                          <div class="summary-value">${escapeHtml(safeText(lastJob.executionMode, "desconhecido"))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Finalizado em</div>
                          <div class="summary-value">${escapeHtml(formatDate(lastJob.finishedAt))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Assignment</div>
                          <div class="summary-value mono">${escapeHtml(safeText(lastJob.assignmentId))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Saida verificada</div>
                          <div class="summary-value">${escapeHtml(lastJob.outputVerified ? "Sim" : "Nao")}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Blender selecionado</div>
                          <div class="summary-value">${escapeHtml(safeText(lastJob.selectedBlenderVersion || worker.blenderVersion))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Categoria / codigo</div>
                          <div class="summary-value">${escapeHtml(safeText([lastJob.failureCategory, lastJob.failureCode].filter(Boolean).join(" / "), "sem classificacao"))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Report ao coordinator</div>
                          <div class="summary-value">${escapeHtml(safeText(coordinatorReport?.status, "nao informado"))}</div>
                        </div>
                      </div>
                      <div class="summary-grid" style="margin-top: 14px;">
                        <div class="summary-item">
                          <div class="summary-label">Erro exato</div>
                          <div class="summary-value">${escapeHtml(truncate(lastJob.error || "Nenhum erro registrado.", 260))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Mensagem</div>
                          <div class="summary-value">${escapeHtml(truncate(lastJob.message || "Sem mensagem adicional.", 260))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Detalhe tecnico</div>
                          <div class="summary-value">${escapeHtml(truncate(lastJob.failureDetails || coordinatorReport?.detail || "Sem detalhe tecnico adicional.", 260))}</div>
                        </div>
                        <div class="summary-item">
                          <div class="summary-label">Frames / tempo</div>
                          <div class="summary-value">${escapeHtml(`${safeText(lastJob.startFrame)}-${safeText(lastJob.endFrame)} | ${safeText(lastJob.elapsedSeconds, 0)}s`)}</div>
                        </div>
                      </div>
                      ${persistentSessionAlert ? `
                        <div class="summary-grid" style="margin-top: 14px;">
                          <div class="summary-item">
                            <div class="summary-label">Sessao persistente</div>
                            <div class="summary-value">${escapeHtml(lastJob.persistentSessionTemporarilyDisabled ? "Temporariamente desativada" : "Ativa")}</div>
                          </div>
                          <div class="summary-item">
                            <div class="summary-label">Motivo</div>
                            <div class="summary-value">${escapeHtml(truncate(lastJob.persistentSessionDisabledReason || "Sem motivo adicional.", 220))}</div>
                          </div>
                          <div class="summary-item">
                            <div class="summary-label">Tentar novamente em</div>
                            <div class="summary-value">${escapeHtml(formatDate(lastJob.persistentSessionRetryAt))}</div>
                          </div>
                        </div>
                      ` : ""}
                      <div class="detail-block" style="margin-top: 14px; padding: 14px;">
                        <h3>Acoes automaticas do worker</h3>
                        ${recoveryActions.length ? `
                          <div class="stack">
                            ${recoveryActions.map((action) => `
                              <div class="list-card">
                                <div class="list-title">${escapeHtml(safeText(action))}</div>
                              </div>
                            `).join("")}
                          </div>
                        ` : emptyState("Este render nao registrou uma acao automatica de recuperacao.")}
                      </div>
                    ` : emptyState("A maquina ainda nao reportou um diagnostico estruturado de ultimo render.")}
                  </div>
                  <div class="detail-block">
                    <h3>Ultimo job reportado pela maquina</h3>
                    ${lastJob ? `<pre>${escapeHtml(JSON.stringify(lastJob, null, 2))}</pre>` : emptyState("A maquina ainda nao reportou um ultimo job estruturado.")}
                  </div>
                  <details>
                    <summary>Capabilities completas</summary>
                    <pre>${escapeHtml(JSON.stringify(capabilities, null, 2))}</pre>
                  </details>
                  <details>
                    <summary>Payload bruto da maquina</summary>
                    <pre>${escapeHtml(JSON.stringify(worker, null, 2))}</pre>
                  </details>
                </div>
              `;
            }

            function renderEvents(logs) {
              const events = Array.isArray(logs?.events) ? logs.events : [];
              const general = safeText(logs?.general, "");
              const container = document.getElementById("eventsPanel");

              if (!events.length && !general) {
                container.innerHTML = emptyState("Ainda nao ha eventos estruturados ou tail do log para exibir.");
                return;
              }

              const eventHtml = events.slice(0, 24).map((event) => `
                <div class="event-line">
                  <div class="event-head">
                    ${renderStatus(event.level || "info")}
                    <span class="muted small">${escapeHtml(safeText(event.category, "geral"))}</span>
                    <span class="muted small">${escapeHtml(formatDate(event.createdAt))}</span>
                    <span class="muted small mono">${escapeHtml(safeText(event.entityId, ""))}</span>
                  </div>
                  <div class="list-title">${escapeHtml(safeText(event.message))}</div>
                  <div class="event-body">${escapeHtml(truncate(event.details || "", 220))}</div>
                </div>
              `).join("");

              const generalHtml = general
                ? `
                    <details>
                      <summary>Tail do log geral</summary>
                      <pre>${escapeHtml(general)}</pre>
                    </details>
                  `
                : "";

              container.innerHTML = `${eventHtml}${generalHtml}`;
            }

            async function refreshDashboard(options = {}) {
              const preserveScroll = options.preserveScroll !== false;
              const scrollPosition = preserveScroll ? captureWindowScroll() : null;
              try {
                const [stateResponse, logsResponse] = await Promise.all([
                  fetch("/state", { cache: "no-store" }),
                  fetch("/logs", { cache: "no-store" }),
                ]);

                if (!stateResponse.ok) {
                  throw new Error(`Falha ao carregar /state: HTTP ${stateResponse.status}`);
                }
                if (!logsResponse.ok) {
                  throw new Error(`Falha ao carregar /logs: HTTP ${logsResponse.status}`);
                }

                appState.snapshot = await stateResponse.json();
                appState.logs = await logsResponse.json();

                renderHero(appState.snapshot);
                renderMetrics(appState.snapshot);
                renderActiveJob(appState.snapshot);
                renderProblems(appState.snapshot, appState.logs);
                renderJobs(appState.snapshot);
                renderWorkers(appState.snapshot);
                renderEvents(appState.logs);

                const jobs = Array.isArray(appState.snapshot.jobs) ? appState.snapshot.jobs : [];
                const workers = Array.isArray(appState.snapshot.workers) ? appState.snapshot.workers : [];

                if (!appState.selectedJobId || !jobs.some((job) => job.jobId === appState.selectedJobId)) {
                  appState.selectedJobId = appState.snapshot?.activeJob?.jobId || (jobs[0] ? jobs[0].jobId : null);
                }
                if (appState.selectedJobId) {
                  await loadJobDetail(appState.selectedJobId, { showLoading: false });
                } else {
                  renderJobDetail(null);
                }

                if (!appState.selectedWorkerId || !workers.some((worker) => worker.workerId === appState.selectedWorkerId)) {
                  appState.selectedWorkerId = workers[0] ? workers[0].workerId : null;
                }
                renderWorkerDetail();

                document.getElementById("lastUpdated").textContent = `Atualizado em ${new Date().toLocaleTimeString("pt-BR")}`;
              } catch (error) {
                const message = error instanceof Error ? error.message : "Falha ao atualizar o dashboard.";
                document.getElementById("eventsPanel").innerHTML = emptyState(message);
              } finally {
                if (preserveScroll) {
                  restoreWindowScroll(scrollPosition);
                }
              }
            }

            document.getElementById("refreshButton").addEventListener("click", () => {
              refreshDashboard({ preserveScroll: false });
            });

            document.getElementById("toggleAutoRefreshButton").addEventListener("click", () => {
              appState.autoRefreshEnabled = !appState.autoRefreshEnabled;
              updateAutoRefreshButton();
            });

            updateAutoRefreshButton();
            refreshDashboard({ preserveScroll: false });
            setInterval(() => {
              if (!appState.autoRefreshEnabled) {
                return;
              }
              refreshDashboard({ preserveScroll: true });
            }, 4000);
          </script>
        </body>
        </html>
        """
    )
