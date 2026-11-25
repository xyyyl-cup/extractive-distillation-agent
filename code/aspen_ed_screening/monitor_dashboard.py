# -*- coding: utf-8 -*-
"""
Local dashboard for monitoring the running Aspen NSGA-II optimization.

The server is read-only: it only reads the optimization workbook, logs, and
process list. It does not control Aspen or the optimizer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKBOOK = BASE_DIR / "outputs" / "optimization" / "nmp_nsga2_tac_formal_100x50.xlsx"
DEFAULT_REFRESH_SECONDS = 8

DECISION_COLUMNS = [
    "case_id",
    "generation",
    "SF",
    "RR1",
    "D1",
    "RR2",
    "D2",
    "N1",
    "NF1",
    "NS1",
    "N2",
    "NF2",
    "x_C3F8_top1",
    "recovery_C3F8",
    "TAC",
    "energy_cost",
    "solvent_makeup_cost",
    "total_reboiler_duty",
    "B1_diameter_m",
    "B2_diameter_m",
    "decision_score",
]

VARIABLE_COLUMNS = ["SF", "RR1", "D1", "RR2", "D2", "N1", "NF1", "NS1", "N2", "NF2"]
OBJECTIVE_COLUMNS = ["recovery_C3F8", "TAC", "energy_cost", "solvent_loss_total"]


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aspen 优化监控台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f9fbfd;
      --ink: #172033;
      --muted: #596579;
      --line: #d8e0ea;
      --line-strong: #b8c4d4;
      --strong: #0d766d;
      --strong-soft: #e7f4f0;
      --warn: #9a5b0b;
      --warn-soft: #fff4df;
      --bad: #b91c1c;
      --bad-soft: #fdecec;
      --blue: #2459b3;
      --blue-soft: #e8effb;
      --shade: #eef3f8;
      --focus: #1d4ed8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.45;
      letter-spacing: 0;
      text-rendering: optimizeLegibility;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(246, 248, 251, 0.96);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(8px);
    }
    .bar {
      max-width: 1480px;
      margin: 0 auto;
      padding: 13px 20px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 16px;
    }
    .title-row {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 720;
    }
    .sub {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      word-break: break-all;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .status-pills {
      align-items: stretch;
    }
    .icon-button {
      width: 38px;
      height: 38px;
      display: inline-grid;
      place-items: center;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 8px;
      cursor: pointer;
      font-size: 18px;
      transition: background-color 0.16s ease, border-color 0.16s ease, color 0.16s ease;
    }
    .icon-button:hover {
      background: var(--blue-soft);
      border-color: #b9cbed;
      color: var(--blue);
    }
    .select, .input {
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      padding: 0 10px;
      min-width: 110px;
    }
    .select:hover, .input:hover {
      border-color: var(--line-strong);
    }
    .icon-button:focus-visible,
    .select:focus-visible,
    .input:focus-visible,
    .text-button:focus-visible {
      outline: 2px solid var(--focus);
      outline-offset: 2px;
    }
    .workbook-select {
      min-width: 240px;
      max-width: min(42vw, 420px);
    }
    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 16px 20px 28px;
    }
    .status-line {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) auto;
      gap: 12px;
      align-items: center;
      margin-bottom: 12px;
    }
    .progress-wrap {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }
    .progress-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    .progress {
      height: 14px;
      border-radius: 999px;
      background: #e5edf6;
      overflow: hidden;
      border: 1px solid #d7e1ee;
    }
    .progress > div {
      height: 100%;
      width: 100%;
      background: var(--strong);
      transform: scaleX(0);
      transform-origin: left center;
      transition: transform 0.24s cubic-bezier(0.22, 1, 0.36, 1);
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .pill-label {
      color: var(--muted);
    }
    .context-pill {
      background: var(--strong-soft);
      border-color: #b7ded4;
      color: #075b52;
      font-weight: 650;
    }
    .pill strong { color: var(--ink); }
    .running { color: var(--strong); }
    .stale { color: var(--warn); }
    .error { color: var(--bad); }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 12px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
    }
    .card-head {
      min-height: 48px;
      padding: 11px 13px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      background: var(--panel-soft);
    }
    .card-title {
      margin: 0;
      font-size: 14px;
      font-weight: 720;
    }
    .card-body { padding: 12px 13px 13px; }
    .kpi { grid-column: span 2; }
    .wide { grid-column: span 8; }
    .side { grid-column: span 4; }
    .half { grid-column: span 6; }
    .full { grid-column: 1 / -1; }
    .kpi .card-body {
      min-height: 92px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .kpi-label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 7px;
    }
    .kpi-value {
      font-size: 24px;
      font-weight: 760;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }
    .kpi-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 9px;
      text-align: right;
      white-space: nowrap;
    }
    th:first-child, td:first-child { text-align: left; }
    th {
      color: #465368;
      font-weight: 650;
      background: var(--panel-soft);
      position: sticky;
      top: 0;
      z-index: 1;
    }
    tbody tr:hover {
      background: #f7fbff;
    }
    .table-scroll {
      max-height: 390px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .param-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(84px, 1fr));
      gap: 8px;
    }
    .param {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: var(--panel-soft);
      min-width: 0;
    }
    .param span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }
    .param strong {
      font-size: 15px;
      overflow-wrap: anywhere;
    }
    .alert-list {
      display: grid;
      gap: 8px;
    }
    .alert {
      border: 1px solid #f1cf86;
      background: var(--warn-soft);
      color: #78350f;
      border-radius: 8px;
      padding: 9px 10px;
      font-size: 12px;
      line-height: 1.5;
    }
    .alert.ok {
      border-color: #b7e4d4;
      background: var(--strong-soft);
      color: #064e3b;
    }
    canvas {
      width: 100%;
      height: 260px;
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
    }
    .paper-figure-wrap {
      display: grid;
      gap: 10px;
    }
    .paper-figure-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .paper-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .text-button {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      padding: 0 11px;
      cursor: pointer;
      font-size: 12px;
      transition: background-color 0.16s ease, border-color 0.16s ease;
    }
    .text-button:hover {
      background: var(--blue-soft);
      border-color: #b9cbed;
    }
    .paper-svg-shell {
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
    }
    .paper-svg-shell svg {
      display: block;
      width: 100%;
      min-width: 760px;
      height: auto;
    }
    pre {
      margin: 0;
      padding: 12px;
      min-height: 160px;
      max-height: 300px;
      overflow: auto;
      background: #111827;
      color: #d1d5db;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .muted { color: var(--muted); }
    .mono { font-family: Consolas, "Cascadia Mono", monospace; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }
    @media (max-width: 1100px) {
      .kpi { grid-column: span 4; }
      .wide, .side, .half { grid-column: 1 / -1; }
      .param-grid { grid-template-columns: repeat(3, minmax(84px, 1fr)); }
      .status-line { grid-template-columns: 1fr; }
    }
    @media (max-width: 680px) {
      .bar {
        grid-template-columns: 1fr;
        align-items: stretch;
      }
      .toolbar { justify-content: flex-start; }
      .kpi { grid-column: span 6; }
      .param-grid { grid-template-columns: repeat(2, minmax(84px, 1fr)); }
      .workbook-select {
        min-width: min(100%, 260px);
        max-width: 100%;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <div class="title-row">
          <h1>Aspen NSGA-II 优化监控台</h1>
          <span class="pill context-pill">只读运行监控</span>
        </div>
        <div class="sub mono" id="workbookPath">等待数据源</div>
      </div>
      <div class="toolbar">
        <label class="pill">查看萃取剂
          <select class="select workbook-select" id="workbookSelect"></select>
        </label>
        <label class="pill">刷新间隔
          <select class="select" id="refreshSec">
            <option value="5">5 s</option>
            <option value="8" selected>8 s</option>
            <option value="15">15 s</option>
            <option value="30">30 s</option>
          </select>
        </label>
        <button class="icon-button" id="refreshBtn" title="立即刷新">↻</button>
      </div>
    </div>
  </header>
  <main>
    <section class="status-line">
      <div class="progress-wrap">
        <div class="progress-meta">
          <span id="progressText">进度读取中...</span>
          <span id="etaText" class="muted">ETA 待计算</span>
        </div>
        <div class="progress"><div id="progressBar"></div></div>
      </div>
      <div class="toolbar status-pills">
        <span class="pill"><span class="pill-label">状态</span> <strong id="runState">待读取</strong></span>
        <span class="pill"><span class="pill-label">PID</span> <strong id="pidText">NA</strong></span>
        <span class="pill"><span class="pill-label">更新</span> <strong id="updatedAt">待读取</strong></span>
      </div>
    </section>

    <section class="grid">
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">已评价</div><div class="kpi-value" id="kEvaluations">NA</div></div><div class="kpi-note" id="kExpected">目标待读取</div></div></div>
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">当前代数</div><div class="kpi-value" id="kGeneration">NA</div></div><div class="kpi-note">generation</div></div></div>
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">收敛数</div><div class="kpi-value" id="kConverged">NA</div></div><div class="kpi-note" id="kConvRate">NA</div></div></div>
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">可行数</div><div class="kpi-value" id="kFeasible">NA</div></div><div class="kpi-note" id="kFeasRate">NA</div></div></div>
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">Pareto 点</div><div class="kpi-value" id="kPareto">NA</div></div><div class="kpi-note">当前非支配解</div></div></div>
      <div class="card kpi"><div class="card-body"><div><div class="kpi-label">最低 TAC</div><div class="kpi-value" id="kMinTac">NA</div></div><div class="kpi-note">USD/year</div></div></div>

      <div class="card half">
        <div class="card-head"><h2 class="card-title">当前推荐折中点</h2><span class="pill" id="bestDecisionCase">NA</span></div>
        <div class="card-body">
          <div class="param-grid" id="bestDecisionParams"></div>
        </div>
      </div>
      <div class="card half">
        <div class="card-head"><h2 class="card-title">最低 TAC 可行点</h2><span class="pill" id="bestTacCase">NA</span></div>
        <div class="card-body">
          <div class="param-grid" id="bestTacParams"></div>
        </div>
      </div>

      <div class="card wide">
        <div class="card-head"><h2 class="card-title">代际趋势</h2><span class="muted">可行点、Pareto、TAC 变化</span></div>
        <div class="card-body"><canvas id="historyChart" width="900" height="260"></canvas></div>
      </div>
      <div class="card side">
        <div class="card-head"><h2 class="card-title">边界检查</h2><span class="muted">判断是否需要扩范围</span></div>
        <div class="card-body"><div class="alert-list" id="boundaryAlerts"></div></div>
      </div>

      <div class="card half">
        <div class="card-head"><h2 class="card-title">Pareto: recovery vs TAC</h2><span class="muted">二维投影</span></div>
        <div class="card-body"><canvas id="paretoChart" width="700" height="260"></canvas></div>
      </div>
      <div class="card half">
        <div class="card-head"><h2 class="card-title">目标最优点</h2><span class="muted">单目标参考，不直接当最终结论</span></div>
        <div class="card-body">
          <div class="table-scroll"><table id="bestObjectivesTable"></table></div>
        </div>
      </div>

      <div class="card full">
        <div class="card-head"><h2 class="card-title">Decision Rank 前 12</h2><span class="muted">按当前权重排序</span></div>
        <div class="card-body"><div class="table-scroll"><table id="decisionTable"></table></div></div>
      </div>

      <div class="card full">
        <div class="card-head">
          <h2 class="card-title">论文图表</h2>
          <div class="paper-actions">
            <span class="muted">Convergence figure</span>
            <button class="text-button" id="downloadConvergenceSvgBtn" title="下载 SVG">下载 SVG</button>
          </div>
        </div>
        <div class="card-body paper-figure-wrap">
          <div id="paperConvergenceFigure" class="paper-svg-shell"></div>
          <div class="paper-figure-note" id="paperConvergenceNote">等待历史数据...</div>
        </div>
      </div>

      <div class="card full">
        <div class="card-head">
          <h2 class="card-title">论文图表</h2>
          <div class="paper-actions">
            <span class="muted">Pareto figure</span>
            <button class="text-button" id="downloadParetoSvgBtn" title="下载 SVG">下载 SVG</button>
          </div>
        </div>
        <div class="card-body paper-figure-wrap">
          <div id="paperParetoFigure" class="paper-svg-shell"></div>
          <div class="paper-figure-note" id="paperParetoNote">等待 Pareto 数据...</div>
        </div>
      </div>

      <div class="card full">
        <div class="card-head"><h2 class="card-title">最新日志</h2><span class="muted mono" id="logPath">NA</span></div>
        <div class="card-body"><pre id="logTail">读取中...</pre></div>
      </div>
    </section>
  </main>

  <script>
    const EMPTY = "NA";
    const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });
    const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
    let timer = null;
    let lastData = null;
    let loadSequence = 0;
    const workbookDataCache = new Map();

    function numberValue(v) {
      return typeof v === "number" && Number.isFinite(v);
    }
    function format(v, digits = 3) {
      if (!numberValue(v)) return EMPTY;
      return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(v);
    }
    function pct(v) {
      if (!numberValue(v)) return EMPTY;
      return `${(v * 100).toFixed(1)}%`;
    }
    function money(v) {
      if (!numberValue(v)) return EMPTY;
      if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(3)} M`;
      return fmt0.format(v);
    }
    function pick(row, keys) {
      const out = {};
      keys.forEach(k => out[k] = row ? row[k] : null);
      return out;
    }
    function setText(id, value, cls) {
      const el = document.getElementById(id);
      el.textContent = value;
      if (cls !== undefined) el.className = cls;
    }
    function makeParams(row) {
      const keys = ["SF","RR1","D1","RR2","D2","N1","NF1","NS1","N2","NF2","recovery_C3F8","TAC","energy_cost","solvent_makeup_cost"];
      return keys.map(k => {
        const label = k === "recovery_C3F8" ? "recovery" : k;
        let value = row ? row[k] : null;
        let text = k === "TAC" || k === "energy_cost" || k === "solvent_makeup_cost" ? money(value) : format(value, k.startsWith("N") ? 0 : 4);
        if (k === "recovery_C3F8") text = format(value, 6);
        return `<div class="param"><span>${label}</span><strong>${text}</strong></div>`;
      }).join("");
    }
    function renderTable(id, rows, cols) {
      const table = document.getElementById(id);
      if (!rows || rows.length === 0) {
        table.innerHTML = `<tbody><tr><td class="muted">暂无数据</td></tr></tbody>`;
        return;
      }
      const head = `<thead><tr>${cols.map(c => `<th>${c}</th>`).join("")}</tr></thead>`;
      const body = rows.map(row => `<tr>${cols.map(c => {
        let v = row[c];
        if (c === "TAC" || c === "energy_cost" || c === "solvent_makeup_cost") v = money(v);
        else if (c === "recovery_C3F8" || c === "x_C3F8_top1") v = format(v, 6);
        else if (numberValue(v)) v = format(v, c.startsWith("N") || c === "generation" ? 0 : 4);
        else if (v === null || v === undefined) v = EMPTY;
        return `<td>${v}</td>`;
      }).join("")}</tr>`).join("");
      table.innerHTML = head + `<tbody>${body}</tbody>`;
    }
    function drawHistory(canvas, history) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, w, h);
      if (!history || history.length === 0) {
        ctx.fillStyle = "#657086";
        ctx.fillText("暂无历史数据", 20, 30);
        return;
      }
      const pad = 42;
      const xs = history.map(r => r.generation);
      const tacVals = history.map(r => r.min_TAC).filter(numberValue);
      const feasVals = history.map(r => r.all_feasible).filter(numberValue);
      const xMin = Math.min(...xs), xMax = Math.max(...xs);
      const tacMin = Math.min(...tacVals), tacMax = Math.max(...tacVals);
      const feasMax = Math.max(...feasVals, 1);
      function x(g) { return pad + (w - pad * 1.6) * ((g - xMin) / Math.max(1, xMax - xMin)); }
      function yTac(v) { return h - pad - (h - pad * 1.6) * ((v - tacMin) / Math.max(1, tacMax - tacMin)); }
      function yFeas(v) { return h - pad - (h - pad * 1.6) * (v / feasMax); }
      ctx.strokeStyle = "#dce3ef";
      ctx.lineWidth = 1;
      for (let i = 0; i < 5; i++) {
        const yy = pad + i * (h - pad * 1.6) / 4;
        ctx.beginPath(); ctx.moveTo(pad, yy); ctx.lineTo(w - pad * 0.6, yy); ctx.stroke();
      }
      function line(key, color, yFn) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        history.forEach((r, i) => {
          const xx = x(r.generation), yy = yFn(r[key]);
          if (i === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
        });
        ctx.stroke();
        ctx.fillStyle = color;
        history.forEach(r => { ctx.beginPath(); ctx.arc(x(r.generation), yFn(r[key]), 3, 0, Math.PI * 2); ctx.fill(); });
      }
      line("min_TAC", "#2563eb", yTac);
      line("all_feasible", "#0f766e", yFeas);
      ctx.fillStyle = "#172033";
      ctx.font = "12px Segoe UI";
      ctx.fillText("blue: min TAC", pad, 18);
      ctx.fillText("green: feasible count", pad + 110, 18);
      ctx.fillStyle = "#657086";
      ctx.fillText(`gen ${xMin}`, pad, h - 12);
      ctx.fillText(`gen ${xMax}`, w - pad * 1.2, h - 12);
    }
    function drawPareto(canvas, points) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, w, h);
      const rows = (points || []).filter(r => numberValue(r.recovery_C3F8) && numberValue(r.TAC));
      if (rows.length === 0) {
        ctx.fillStyle = "#657086";
        ctx.fillText("暂无 Pareto 数据", 20, 30);
        return;
      }
      const pad = 44;
      const xs = rows.map(r => r.recovery_C3F8);
      const ys = rows.map(r => r.TAC);
      const xMin = Math.min(...xs), xMax = Math.max(...xs);
      const yMin = Math.min(...ys), yMax = Math.max(...ys);
      function x(v) { return pad + (w - pad * 1.6) * ((v - xMin) / Math.max(1e-9, xMax - xMin)); }
      function y(v) { return h - pad - (h - pad * 1.6) * ((v - yMin) / Math.max(1, yMax - yMin)); }
      ctx.strokeStyle = "#dce3ef";
      ctx.beginPath(); ctx.moveTo(pad, pad * 0.5); ctx.lineTo(pad, h - pad); ctx.lineTo(w - pad * 0.6, h - pad); ctx.stroke();
      rows.forEach((r, i) => {
        ctx.fillStyle = i === 0 ? "#b45309" : "#2563eb";
        ctx.beginPath(); ctx.arc(x(r.recovery_C3F8), y(r.TAC), i === 0 ? 5 : 4, 0, Math.PI * 2); ctx.fill();
      });
      ctx.fillStyle = "#657086";
      ctx.font = "12px Segoe UI";
      ctx.fillText("Recovery", w - 100, h - 12);
      ctx.save();
      ctx.translate(14, 112);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText("TAC", 0, 0);
      ctx.restore();
    }
    function escapeXml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&apos;");
    }
    function paperImprovementGenerations(rows, key) {
      const out = [];
      let previous = null;
      rows.forEach(row => {
        const value = row[key];
        if (!numberValue(value)) return;
        if (previous === null || Math.abs(value - previous) > Math.max(1e-9, Math.abs(previous) * 1e-10)) {
          out.push(row.generation);
          previous = value;
        }
      });
      return out;
    }
    function paperTicks(minV, maxV, count = 3) {
      if (!numberValue(minV) || !numberValue(maxV)) return [];
      if (Math.abs(maxV - minV) < 1e-12) return [minV];
      const rawStep = (maxV - minV) / Math.max(1, count - 1);
      const power = Math.pow(10, Math.floor(Math.log10(rawStep)));
      const multiples = [1, 2, 2.5, 5, 10];
      const step = multiples.map(m => m * power).find(v => v >= rawStep) || 10 * power;
      const start = Math.floor(minV / step) * step;
      const end = Math.ceil(maxV / step) * step;
      const ticks = [];
      for (let value = start; value <= end + step * 0.25; value += step) ticks.push(value);
      return ticks;
    }
    function formatPaperValue(value, digits = 1) {
      return new Intl.NumberFormat("en-US", {
        minimumFractionDigits: 0,
        maximumFractionDigits: digits,
      }).format(value);
    }
    function buildPaperConvergenceSvg(history) {
      const rows = (history || []).filter(r =>
        numberValue(r.generation)
        && numberValue(r.min_TAC)
        && numberValue(r.min_energy_cost)
        && numberValue(r.min_total_reboiler_duty_kW)
      );
      if (!rows.length) {
        return `<svg viewBox="0 0 1200 260" xmlns="http://www.w3.org/2000/svg">
          <rect width="1200" height="260" fill="#ffffff"/>
          <text x="48" y="72" font-family="Arial, sans-serif" font-size="20" fill="#64748b">No convergence history available</text>
        </svg>`;
      }

      const series = [
        { key: "min_TAC", label: "TAC", color: "#2563eb", dash: "", marker: "circle" },
        { key: "min_energy_cost", label: "Energy cost", color: "#dc2626", dash: "10 7", marker: "square" },
        { key: "min_total_reboiler_duty_kW", label: "Total reboiler duty", color: "#059669", dash: "3 7", marker: "triangle" },
      ];
      const xMin = rows[0].generation;
      const xMax = rows[rows.length - 1].generation;
      const width = 1200;
      const height = 640;
      const left = 116;
      const right = 42;
      const top = 36;
      const bottom = 86;
      const xTicks = Array.from(new Set([0, 20, 40, 60, 80, 100, xMax].filter(v => v >= xMin && v <= xMax))).sort((a, b) => a - b);
      const x = generation => left + (width - left - right) * ((generation - xMin) / Math.max(1, xMax - xMin));
      const percentRows = rows.map(row => {
        const out = { generation: row.generation };
        series.forEach(item => {
          const base = rows[0][item.key];
          out[item.key] = base ? ((base - row[item.key]) / base) * 100 : 0;
        });
        return out;
      });
      const maxImprovement = Math.max(
        ...series.flatMap(item => percentRows.map(row => row[item.key]).filter(numberValue))
      );
      const yMax = Math.ceil((maxImprovement + 1) / 5) * 5;
      const yTicks = [];
      for (let value = 0; value <= yMax; value += 5) yTicks.push(value);
      const y = value => top + (height - top - bottom) - (height - top - bottom) * (value / Math.max(1, yMax));

      let svg = `<svg id="paperConvergenceSvg" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Convergence of key objectives">
        <rect width="${width}" height="${height}" fill="#ffffff"/>
        <style>
          text { font-family: "Times New Roman", Times, serif; fill: #000000; }
          .axis { stroke: #111827; stroke-width: 1.4; }
          .paper-gridline { stroke: #000000; stroke-width: 0.6; opacity: 0.16; }
          .curve { fill: none; stroke-width: 2.6; stroke-linejoin: round; stroke-linecap: round; }
        </style>`;

      yTicks.forEach(tick => {
        const yy = y(tick);
        svg += `<line class="paper-gridline" x1="${left}" y1="${yy.toFixed(2)}" x2="${width - right}" y2="${yy.toFixed(2)}"/>`;
        svg += `<text x="${left - 14}" y="${(yy + 6).toFixed(2)}" text-anchor="end" font-size="20">${tick}</text>`;
      });
      svg += `<line class="axis" x1="${left}" y1="${top}" x2="${left}" y2="${height - bottom}"/>`;
      svg += `<line class="axis" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"/>`;

      function markerSvg(marker, cx, cy, color) {
        if (marker === "square") {
          return `<rect x="${(cx - 4.5).toFixed(2)}" y="${(cy - 4.5).toFixed(2)}" width="9" height="9" fill="${color}"/>`;
        }
        if (marker === "triangle") {
          return `<polygon points="${cx.toFixed(2)},${(cy - 5.5).toFixed(2)} ${(cx - 5.5).toFixed(2)},${(cy + 4.5).toFixed(2)} ${(cx + 5.5).toFixed(2)},${(cy + 4.5).toFixed(2)}" fill="${color}"/>`;
        }
        return `<circle cx="${cx.toFixed(2)}" cy="${cy.toFixed(2)}" r="4.8" fill="${color}"/>`;
      }

      series.forEach(item => {
        let path = "";
        percentRows.forEach((row, rowIndex) => {
          const xx = x(row.generation);
          const yy = y(row[item.key]);
          if (rowIndex === 0) {
            path = `M ${xx.toFixed(2)} ${yy.toFixed(2)}`;
          } else {
            path += ` H ${xx.toFixed(2)} V ${yy.toFixed(2)}`;
          }
        });
        svg += `<path class="curve" stroke="${item.color}" ${item.dash ? `stroke-dasharray="${item.dash}"` : ""} d="${path}"/>`;

        const improvementGens = new Set(paperImprovementGenerations(rows, item.key));
        percentRows.filter(row => improvementGens.has(row.generation)).forEach(row => {
          svg += markerSvg(item.marker, x(row.generation), y(row[item.key]), item.color);
        });
      });

      xTicks.forEach(tick => {
        const xx = x(tick);
        svg += `<line class="axis" x1="${xx.toFixed(2)}" y1="${height - bottom}" x2="${xx.toFixed(2)}" y2="${height - bottom + 8}"/>`;
        svg += `<text x="${xx.toFixed(2)}" y="${height - bottom + 36}" text-anchor="middle" font-size="20">${tick}</text>`;
      });
      svg += `<text x="${((left + width - right) / 2).toFixed(2)}" y="${height - 18}" text-anchor="middle" font-size="24">Generation</text>`;
      svg += `<g transform="translate(34 ${(top + height - bottom) / 2}) rotate(-90)">
        <text text-anchor="middle" font-size="24">Relative improvement (%)</text>
      </g>`;
      const legendY = 34;
      const legendStartX = left;
      series.forEach((item, index) => {
        const lx = legendStartX + index * 280;
        svg += `<line x1="${lx}" y1="${legendY}" x2="${lx + 48}" y2="${legendY}" stroke="${item.color}" stroke-width="2.6" ${item.dash ? `stroke-dasharray="${item.dash}"` : ""}/>`;
        svg += markerSvg(item.marker, lx + 24, legendY, item.color);
        svg += `<text x="${lx + 62}" y="${legendY + 7}" font-size="21">${escapeXml(item.label)}</text>`;
      });
      svg += `</svg>`;
      return svg;
    }
    function updatePaperConvergenceNote(history) {
      const note = document.getElementById("paperConvergenceNote");
      const rows = history || [];
      if (!rows.length) {
        note.textContent = "暂无收敛历史数据。";
        return;
      }
      const first = rows[0];
      const last = rows[rows.length - 1];
      const tacDrop = ((first.min_TAC - last.min_TAC) / first.min_TAC) * 100;
      const energyDrop = ((first.min_energy_cost - last.min_energy_cost) / first.min_energy_cost) * 100;
      const dutyDrop = ((first.min_total_reboiler_duty_kW - last.min_total_reboiler_duty_kW) / first.min_total_reboiler_duty_kW) * 100;
      note.textContent = `Final relative improvement: TAC ${format(tacDrop, 2)}%, energy cost ${format(energyDrop, 2)}%, total reboiler duty ${format(dutyDrop, 2)}%.`;
    }
    function buildPaperParetoSvg(points, decision) {
      const rows = (points || []).filter(row =>
        numberValue(row.recovery_C3F8)
        && numberValue(row.TAC)
        && numberValue(row.energy_cost)
      );
      if (!rows.length) {
        return `<svg viewBox="0 0 1200 260" xmlns="http://www.w3.org/2000/svg">
          <rect width="1200" height="260" fill="#ffffff"/>
          <text x="48" y="72" font-family="Times New Roman, Times, serif" font-size="20" fill="#000000">No Pareto data available</text>
        </svg>`;
      }

      const width = 1200;
      const height = 650;
      const left = 102;
      const right = 38;
      const top = 44;
      const bottom = 78;
      const gap = 74;
      const panelW = (width - left - right - gap) / 2;
      const tacMin = Math.min(...rows.map(row => row.TAC / 10000));
      const tacMax = Math.max(...rows.map(row => row.TAC / 10000));
      const tacTicks = paperTicks(tacMin, tacMax, 5);
      const minTac = rows.reduce((best, row) => row.TAC < best.TAC ? row : best, rows[0]);
      const decisionCase = decision && decision.case_id ? rows.find(row => row.case_id === decision.case_id) : null;

      const panels = [
        {
          label: "(a)",
          xLabel: "Recovery of C3F8 (%)",
          xKey: "recovery_C3F8",
          xScale: value => value * 100,
          xValues: rows.map(row => row.recovery_C3F8 * 100),
          xDigits: 4,
        },
        {
          label: "(b)",
          xLabel: "Energy cost (10⁴ USD/year)",
          xKey: "energy_cost",
          xScale: value => value / 10000,
          xValues: rows.map(row => row.energy_cost / 10000),
          xDigits: 1,
        },
      ];

      let svg = `<svg id="paperParetoSvg" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Pareto projections">
        <rect width="${width}" height="${height}" fill="#ffffff"/>
        <style>
          text { font-family: "Times New Roman", Times, serif; fill: #000000; }
          .axis { stroke: #111827; stroke-width: 1.4; }
          .paper-gridline { stroke: #000000; stroke-width: 0.6; opacity: 0.16; }
          .pareto-point { fill: #2563eb; }
          .decision { fill: #dc2626; stroke: #dc2626; stroke-width: 1.8; }
          .minimum { fill: none; stroke: #059669; stroke-width: 2.2; }
        </style>`;

      panels.forEach((panel, index) => {
        const x0 = left + index * (panelW + gap);
        const xMin = Math.min(...panel.xValues);
        const xMax = Math.max(...panel.xValues);
        const xPad = Math.max((xMax - xMin) * 0.08, 0.02);
        const yPad = Math.max((tacMax - tacMin) * 0.08, 0.3);
        const lowX = xMin - xPad;
        const highX = xMax + xPad;
        const lowY = tacMin - yPad;
        const highY = tacMax + yPad;
        const x = value => x0 + panelW * ((value - lowX) / Math.max(1e-9, highX - lowX));
        const y = value => top + (height - top - bottom) - (height - top - bottom) * ((value - lowY) / Math.max(1e-9, highY - lowY));
        const xTicks = paperTicks(xMin, xMax, 5);

        tacTicks.forEach(tick => {
          const yy = y(tick);
          svg += `<line class="paper-gridline" x1="${x0}" y1="${yy.toFixed(2)}" x2="${(x0 + panelW).toFixed(2)}" y2="${yy.toFixed(2)}"/>`;
          if (index === 0) {
            svg += `<text x="${x0 - 14}" y="${(yy + 6).toFixed(2)}" text-anchor="end" font-size="20">${formatPaperValue(tick, 1)}</text>`;
          }
        });
        xTicks.forEach(tick => {
          const xx = x(tick);
          svg += `<line class="axis" x1="${xx.toFixed(2)}" y1="${height - bottom}" x2="${xx.toFixed(2)}" y2="${height - bottom + 8}"/>`;
          svg += `<text x="${xx.toFixed(2)}" y="${height - bottom + 36}" text-anchor="middle" font-size="20">${formatPaperValue(tick, panel.xDigits)}</text>`;
        });
        svg += `<line class="axis" x1="${x0}" y1="${top}" x2="${x0}" y2="${height - bottom}"/>`;
        svg += `<line class="axis" x1="${x0}" y1="${height - bottom}" x2="${(x0 + panelW).toFixed(2)}" y2="${height - bottom}"/>`;
        svg += `<text x="${x0}" y="${top - 14}" font-size="23" font-weight="600">${panel.label}</text>`;
        svg += `<text x="${(x0 + panelW / 2).toFixed(2)}" y="${height - 20}" text-anchor="middle" font-size="23">${escapeXml(panel.xLabel)}</text>`;

        rows.forEach(row => {
          svg += `<circle class="pareto-point" cx="${x(panel.xScale(row[panel.xKey])).toFixed(2)}" cy="${y(row.TAC / 10000).toFixed(2)}" r="5"/>`;
        });
        svg += `<circle class="minimum" cx="${x(panel.xScale(minTac[panel.xKey])).toFixed(2)}" cy="${y(minTac.TAC / 10000).toFixed(2)}" r="8.2"/>`;
        if (decisionCase) {
          const cx = x(panel.xScale(decisionCase[panel.xKey]));
          const cy = y(decisionCase.TAC / 10000);
          svg += `<path class="decision" d="M ${(cx - 7).toFixed(2)} ${cy.toFixed(2)} L ${(cx + 7).toFixed(2)} ${cy.toFixed(2)} M ${cx.toFixed(2)} ${(cy - 7).toFixed(2)} L ${cx.toFixed(2)} ${(cy + 7).toFixed(2)}"/>`;
        }
      });

      svg += `<g transform="translate(32 ${(top + height - bottom) / 2}) rotate(-90)">
        <text text-anchor="middle" font-size="24">TAC (10⁴ USD/year)</text>
      </g>`;
      const legendY = 32;
      svg += `<circle class="pareto-point" cx="${left}" cy="${legendY}" r="5"/>`;
      svg += `<text x="${left + 16}" y="${legendY + 7}" font-size="21">Pareto solution</text>`;
      svg += `<path class="decision" d="M ${left + 220} ${legendY} L ${left + 234} ${legendY} M ${left + 227} ${legendY - 7} L ${left + 227} ${legendY + 7}"/>`;
      svg += `<text x="${left + 244}" y="${legendY + 7}" font-size="21">TOPSIS compromise point</text>`;
      svg += `<circle class="minimum" cx="${left + 510}" cy="${legendY}" r="8.2"/>`;
      svg += `<text x="${left + 526}" y="${legendY + 7}" font-size="21">Minimum TAC point</text>`;
      svg += `</svg>`;
      return svg;
    }
    function updatePaperParetoNote(points, decision) {
      const note = document.getElementById("paperParetoNote");
      const rows = points || [];
      if (!rows.length) {
        note.textContent = "暂无 Pareto 数据。";
        return;
      }
      const minTac = rows.reduce((best, row) => row.TAC < best.TAC ? row : best, rows[0]);
      note.textContent = decision
        ? `TOPSIS compromise point: ${decision.case_id}; minimum TAC point: ${minTac.case_id}.`
        : `Minimum TAC point: ${minTac.case_id}.`;
    }
    function downloadSvg(svg, filename) {
      const link = document.createElement("a");
      link.download = filename;
      link.href = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
      link.click();
    }
    function render(data) {
      lastData = data;
      const workbookKey = selectedWorkbook();
      if (workbookKey) {
        workbookDataCache.set(workbookKey, data);
      }
      const s = data.summary || {};
      const progress = numberValue(s.progress_fraction) ? Math.max(0, Math.min(1, s.progress_fraction)) : 0;
      setText("workbookPath", data.workbook || "等待数据源");
      setText("progressText", `${format(s.evaluations, 0)} / ${format(s.expected_total, 0)} evaluations (${pct(progress)})`);
      setText("etaText", s.eta_text || "ETA 待计算");
      document.getElementById("progressBar").style.transform = `scaleX(${progress.toFixed(4)})`;
      const running = data.process && data.process.running;
      setText("runState", running ? "RUNNING" : (s.complete ? "DONE" : "NO PROCESS"), running ? "running" : (s.complete ? "" : "stale"));
      setText("pidText", data.process && data.process.pids && data.process.pids.length ? data.process.pids.join(", ") : EMPTY);
      setText("updatedAt", data.generated_at || "待读取");
      setText("kEvaluations", format(s.evaluations, 0));
      setText("kExpected", `目标 ${format(s.expected_total, 0)}`);
      setText("kGeneration", `${format(s.current_generation, 0)} / ${format(s.total_generations, 0)}`);
      setText("kConverged", format(s.converged, 0));
      setText("kConvRate", pct(s.converged_rate));
      setText("kFeasible", format(s.feasible, 0));
      setText("kFeasRate", pct(s.feasible_rate));
      setText("kPareto", format(s.pareto_count, 0));
      setText("kMinTac", money(s.min_TAC));
      const decision = data.best && data.best.decision;
      const tac = data.best && data.best.min_TAC;
      setText("bestDecisionCase", decision ? decision.case_id : EMPTY);
      setText("bestTacCase", tac ? tac.case_id : EMPTY);
      document.getElementById("bestDecisionParams").innerHTML = makeParams(decision);
      document.getElementById("bestTacParams").innerHTML = makeParams(tac);
      const bestCols = ["name","case_id","generation","recovery_C3F8","TAC","energy_cost","solvent_loss_total"];
      renderTable("bestObjectivesTable", data.best_objectives || [], bestCols);
      const decisionCols = ["case_id","generation","SF","RR1","D1","RR2","D2","N1","NF1","NS1","N2","NF2","recovery_C3F8","TAC","energy_cost","decision_score"];
      renderTable("decisionTable", data.decision_rank || [], decisionCols);
      drawHistory(document.getElementById("historyChart"), data.history || []);
      drawPareto(document.getElementById("paretoChart"), data.pareto || []);
      const paperHistory = data.paper_history || data.history || [];
      document.getElementById("paperConvergenceFigure").innerHTML = buildPaperConvergenceSvg(paperHistory);
      updatePaperConvergenceNote(paperHistory);
      document.getElementById("paperParetoFigure").innerHTML = buildPaperParetoSvg(data.pareto || [], decision);
      updatePaperParetoNote(data.pareto || [], decision);
      const alerts = document.getElementById("boundaryAlerts");
      const boundary = data.boundary_alerts || [];
      if (boundary.length === 0) {
        alerts.innerHTML = `<div class="alert ok">当前 Pareto 点没有明显贴住变量边界。正式跑完后再复查一次。</div>`;
      } else {
        alerts.innerHTML = boundary.map(a => `<div class="alert">${a}</div>`).join("");
      }
      setText("logPath", data.logs && data.logs.stdout_path ? data.logs.stdout_path : EMPTY);
      document.getElementById("logTail").textContent = data.logs && data.logs.stdout_tail ? data.logs.stdout_tail : "暂无日志";
    }
    function currentToken() {
      const url = new URL(window.location.href);
      const fromUrl = url.searchParams.get("token");
      if (fromUrl) {
        localStorage.setItem("aspen_dashboard_token", fromUrl);
        return fromUrl;
      }
      return localStorage.getItem("aspen_dashboard_token") || "";
    }
    function currentWorkbook() {
      const url = new URL(window.location.href);
      const fromUrl = url.searchParams.get("workbook");
      if (fromUrl) {
        localStorage.setItem("aspen_dashboard_workbook", fromUrl);
        return fromUrl;
      }
      return localStorage.getItem("aspen_dashboard_workbook") || "";
    }
    function selectedWorkbook() {
      const select = document.getElementById("workbookSelect");
      return select && select.value ? select.value : currentWorkbook();
    }
    function selectedWorkbookLabel() {
      const select = document.getElementById("workbookSelect");
      if (!select || select.selectedIndex < 0) return "结果";
      return select.options[select.selectedIndex].textContent || "结果";
    }
    function setWorkbookSelection(workbook) {
      const url = new URL(window.location.href);
      if (workbook) {
        url.searchParams.set("workbook", workbook);
        localStorage.setItem("aspen_dashboard_workbook", workbook);
      } else {
        url.searchParams.delete("workbook");
        localStorage.removeItem("aspen_dashboard_workbook");
      }
      history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
    async function loadWorkbookOptions() {
      const token = currentToken();
      const suffix = token ? `&token=${encodeURIComponent(token)}` : "";
      const response = await fetch(`/api/workbooks?ts=${Date.now()}${suffix}`);
      if (response.status === 401 || response.status === 403) {
        throw new Error("token missing or invalid");
      }
      const payload = await response.json();
      const rows = payload.workbooks || [];
      const select = document.getElementById("workbookSelect");
      const wanted = currentWorkbook() || payload.default_workbook || "";
      select.innerHTML = rows.map(row => {
        const selected = row.value === wanted ? " selected" : "";
        return `<option value="${escapeXml(row.value)}"${selected}>${escapeXml(row.label)}</option>`;
      }).join("");
      if (!select.value && rows.length) {
        select.value = rows[0].value;
      }
      if (select.value) {
        setWorkbookSelection(select.value);
      }
    }
    async function load() {
      const sequence = ++loadSequence;
      setText("runState", `读取 ${selectedWorkbookLabel()}...`, "stale");
      try {
        const token = currentToken();
        const suffix = token ? `&token=${encodeURIComponent(token)}` : "";
        const workbook = selectedWorkbook();
        const workbookArg = workbook ? `&workbook=${encodeURIComponent(workbook)}` : "";
        const response = await fetch(`/api/status?ts=${Date.now()}${workbookArg}${suffix}`);
        if (response.status === 401 || response.status === 403) {
          throw new Error("token missing or invalid");
        }
        const data = await response.json();
        if (data.error && !data.summary) {
          throw new Error(data.error);
        }
        if (sequence !== loadSequence) {
          return;
        }
        render(data);
      } catch (err) {
        if (sequence !== loadSequence) {
          return;
        }
        setText("runState", "READ ERROR", "error");
        document.getElementById("logTail").textContent = `读取失败：${err.message}\n\n如果 Excel 正在保存，等下一次自动刷新即可。`;
      }
    }
    function schedule() {
      if (timer) clearInterval(timer);
      const sec = Number(document.getElementById("refreshSec").value) || 8;
      timer = setInterval(load, sec * 1000);
    }
    document.getElementById("refreshBtn").addEventListener("click", load);
    document.getElementById("refreshSec").addEventListener("change", schedule);
    document.getElementById("workbookSelect").addEventListener("change", event => {
      setWorkbookSelection(event.target.value);
      const cached = workbookDataCache.get(event.target.value);
      if (cached) {
        render(cached);
      }
      load();
    });
    document.getElementById("downloadConvergenceSvgBtn").addEventListener("click", () => {
      downloadSvg(document.getElementById("paperConvergenceFigure").innerHTML, "nmp_convergence.svg");
    });
    document.getElementById("downloadParetoSvgBtn").addEventListener("click", () => {
      downloadSvg(document.getElementById("paperParetoFigure").innerHTML, "nmp_pareto.svg");
    });
    schedule();
    loadWorkbookOptions().then(load).catch(load);
  </script>
</body>
</html>
"""


def finite_float(value):
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    return value


def bool_series(series):
    if series is None:
        return pd.Series(dtype=bool)
    return series.fillna(False).astype(bool)


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, (float, int)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return clean_value(value.item())
    return value


def clean_record(record):
    return {str(key): clean_value(value) for key, value in record.items()}


def read_sheet(path, sheet_name):
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


def read_workbook_sheets(path, sheet_names):
    frames = {name: pd.DataFrame() for name in sheet_names}
    try:
        with pd.ExcelFile(path) as workbook:
            available = set(workbook.sheet_names)
            for sheet_name in sheet_names:
                if sheet_name in available:
                    frames[sheet_name] = pd.read_excel(workbook, sheet_name=sheet_name)
    except Exception:
        pass
    return frames


def default_checkpoint_path(workbook):
    return Path(workbook).with_suffix(".checkpoint.jsonl")


def read_checkpoint_dataframe(workbook):
    checkpoint = default_checkpoint_path(workbook)
    if not checkpoint.exists():
        return pd.DataFrame()
    rows = []
    with checkpoint.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append({key: clean_value(value) for key, value in payload.items() if not str(key).startswith("_")})
    return pd.DataFrame(rows)


def nondominated_feasible(df):
    if df is None or df.empty or "feasible" not in df.columns:
        return pd.DataFrame()
    feasible = df[bool_series(df["feasible"])].copy()
    if feasible.empty:
        return pd.DataFrame()
    required = ["recovery_C3F8", "TAC", "energy_cost"]
    for column in required:
        if column not in feasible.columns:
            return pd.DataFrame()
        feasible[column] = pd.to_numeric(feasible[column], errors="coerce")
    feasible = feasible.dropna(subset=required)
    if feasible.empty:
        return pd.DataFrame()
    records = feasible.to_dict(orient="records")
    keep = []
    for i, row in enumerate(records):
        a = [1.0 - row["recovery_C3F8"], row["TAC"], row["energy_cost"]]
        dominated = False
        for j, other in enumerate(records):
            if i == j:
                continue
            b = [1.0 - other["recovery_C3F8"], other["TAC"], other["energy_cost"]]
            if all(bv <= av for av, bv in zip(a, b)) and any(bv < av for av, bv in zip(a, b)):
                dominated = True
                break
        if not dominated:
            keep.append(row)
    return pd.DataFrame(keep)


def select_records(df, columns, limit=12):
    if df is None or df.empty:
        return []
    available = [col for col in columns if col in df.columns]
    return [clean_record(row) for row in df[available].head(limit).to_dict(orient="records")]


def settings_dict(settings_df):
    if settings_df is None or settings_df.empty:
        return {}
    if "setting" not in settings_df.columns or "value" not in settings_df.columns:
        return {}
    return {
        str(row["setting"]): clean_value(row["value"])
        for _, row in settings_df.iterrows()
        if pd.notna(row.get("setting"))
    }


def parse_range(value):
    if value is None:
        return None
    parts = str(value).split(",")
    if len(parts) != 2:
        return None
    low = finite_float(parts[0].strip())
    high = finite_float(parts[1].strip())
    if low is None or high is None:
        return None
    return low, high


def get_ranges(settings):
    ranges = {}
    for name in VARIABLE_COLUMNS:
        parsed = parse_range(settings.get(f"{name}_range"))
        if parsed:
            ranges[name] = parsed
    return ranges


def sort_by_numeric(df, column, ascending=True):
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.dropna(subset=[column]).sort_values(column, ascending=ascending)


def best_row(df, column, ascending=True):
    sorted_df = sort_by_numeric(df, column, ascending=ascending)
    if sorted_df.empty:
        return None
    return clean_record(sorted_df.iloc[0].to_dict())


def make_best_objectives(feasible_df):
    specs = [
        ("Min TAC", "TAC", True),
        ("Max recovery", "recovery_C3F8", False),
        ("Min energy", "energy_cost", True),
        ("Min solvent loss", "solvent_loss_total", True),
    ]
    rows = []
    for name, column, ascending in specs:
        record = best_row(feasible_df, column, ascending)
        if record:
            record = {key: record.get(key) for key in DECISION_COLUMNS + ["solvent_loss_total"]}
            record["name"] = name
            rows.append(record)
    return rows


def make_paper_history(all_df):
    required = [
        "generation",
        "feasible",
        "TAC",
        "energy_cost",
        "total_reboiler_duty",
        "total_reboiler_duty_kW",
    ]
    if all_df is None or all_df.empty or any(column not in all_df.columns for column in required):
        return []

    work = all_df.copy()
    work["generation"] = pd.to_numeric(work["generation"], errors="coerce")
    work = work.dropna(subset=["generation"])
    if work.empty:
        return []
    work["generation"] = work["generation"].astype(int)
    feasible = work[bool_series(work["feasible"])].copy()
    if feasible.empty:
        return []

    for column in ["TAC", "energy_cost", "total_reboiler_duty", "total_reboiler_duty_kW"]:
        feasible[column] = pd.to_numeric(feasible[column], errors="coerce")

    grouped = feasible.groupby("generation", as_index=True)[
        ["TAC", "energy_cost", "total_reboiler_duty", "total_reboiler_duty_kW"]
    ].min()
    generations = range(int(work["generation"].min()), int(work["generation"].max()) + 1)
    grouped = grouped.reindex(generations)
    grouped = grouped.cummin().ffill()
    grouped = grouped.reset_index().rename(
        columns={
            "index": "generation",
            "TAC": "min_TAC",
            "energy_cost": "min_energy_cost",
            "total_reboiler_duty": "min_total_reboiler_duty",
            "total_reboiler_duty_kW": "min_total_reboiler_duty_kW",
        }
    )
    return [clean_record(row) for row in grouped.to_dict(orient="records")]


def boundary_alerts(pareto_df, ranges):
    if pareto_df is None or pareto_df.empty or not ranges:
        return []
    alerts = []
    for name, (low, high) in ranges.items():
        if name not in pareto_df.columns:
            continue
        values = pd.to_numeric(pareto_df[name], errors="coerce").dropna()
        if values.empty:
            continue
        span = max(high - low, 1.0)
        tol = max(span * 0.01, 1.0e-6)
        low_hits = int((values <= low + tol).sum())
        high_hits = int((values >= high - tol).sum())
        if low_hits:
            alerts.append(f"{name} 有 {low_hits} 个 Pareto 点贴近下限 {low:g}，正式结果可能需要向下扩边界。")
        if high_hits:
            alerts.append(f"{name} 有 {high_hits} 个 Pareto 点贴近上限 {high:g}，正式结果可能需要向上扩边界。")
    return alerts


def read_tail(path, lines=30):
    if not path or not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192), os.SEEK_SET)
            data = handle.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception as exc:
        return f"Could not read log: {exc}"


def latest_log_pair(output_dir, workbook=None):
    prefix = None
    if workbook is not None:
        stem = Path(workbook).stem.lower()
        if "_nsga2" in stem:
            prefix = stem.split("_nsga2", 1)[0]
    out_logs = []
    if prefix:
        out_logs = sorted(output_dir.glob("{}_*.out.log".format(prefix)), key=lambda p: p.stat().st_mtime, reverse=True)
    if not out_logs:
        out_logs = sorted(output_dir.glob("nmp_formal_100x50_*.out.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not out_logs:
        out_logs = sorted(output_dir.glob("*.out.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    stdout = out_logs[0] if out_logs else None
    stderr = None
    if stdout:
        candidate = stdout.with_name(stdout.name.replace(".out.log", ".err.log"))
        if candidate.exists():
            stderr = candidate
    return stdout, stderr


def find_optimizer_processes(workbook):
    script = (
        "$needle = 'optimize_nmp_nsga2.py'; "
        "$wb = '" + workbook.name.replace("'", "''") + "'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ProcessName -like '*python*' -and $_.CommandLine -like \"*$needle*\" -and $_.CommandLine -like \"*$wb*\" } | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        text = (result.stdout or "").strip()
        if not text:
            return []
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        return [
            {"pid": item.get("ProcessId"), "command": item.get("CommandLine")}
            for item in data
            if item.get("ProcessId")
        ]
    except Exception:
        return []


def seconds_to_text(seconds):
    if seconds is None or seconds < 0 or not math.isfinite(seconds):
        return "ETA 待计算"
    minutes = seconds / 60.0
    if minutes < 1:
        return "ETA <1 min"
    if minutes < 90:
        return f"ETA {minutes:.0f} min"
    hours = minutes / 60.0
    return f"ETA {hours:.1f} h"


def generations_from_name(workbook):
    stem = Path(workbook).stem.lower()
    if "_100x" not in stem:
        return None
    try:
        tail = stem.split("_100x", 1)[1]
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        return int(digits) if digits else None
    except Exception:
        return None


def build_status(workbook):
    workbook = Path(workbook)
    output_dir = workbook.parent
    checkpoint = default_checkpoint_path(workbook)
    workbook_mtime = workbook.stat().st_mtime if workbook.exists() else 0
    checkpoint_mtime = checkpoint.stat().st_mtime if checkpoint.exists() else 0
    checkpoint_is_newer = checkpoint.exists() and checkpoint_mtime > workbook_mtime + 2
    workbook_sheet_names = ["pareto", "decision_rank", "history", "settings"]
    if not checkpoint_is_newer:
        workbook_sheet_names = ["all_evaluations"] + workbook_sheet_names
    sheets = read_workbook_sheets(
        workbook,
        workbook_sheet_names,
    )
    all_df = sheets.get("all_evaluations", pd.DataFrame())
    checkpoint_used = False
    checkpoint_df = read_checkpoint_dataframe(workbook)
    if not checkpoint_df.empty:
        if all_df.empty or checkpoint_is_newer:
            all_df = checkpoint_df
            checkpoint_used = True
    pareto_df = sheets["pareto"]
    decision_df = sheets["decision_rank"]
    history_df = sheets["history"]
    settings_df = sheets["settings"]

    settings = settings_dict(settings_df)
    population = finite_float(settings.get("population_size"))
    generations = finite_float(settings.get("generations"))
    name_generations = generations_from_name(workbook)
    if name_generations is not None and (generations is None or name_generations > generations):
        generations = name_generations
    expected = int(population * (generations + 1)) if population is not None and generations is not None else None

    if pareto_df.empty and not all_df.empty and "rank" in all_df.columns and "feasible" in all_df.columns:
        pareto_df = all_df[(bool_series(all_df["feasible"])) & (pd.to_numeric(all_df["rank"], errors="coerce") == 0)]
    if pareto_df.empty and not all_df.empty:
        pareto_df = nondominated_feasible(all_df)
    if decision_df.empty and not all_df.empty and "feasible" in all_df.columns:
        feasible_tmp = all_df[bool_series(all_df["feasible"])]
        if "decision_score" in feasible_tmp.columns:
            decision_df = sort_by_numeric(feasible_tmp, "decision_score", ascending=False)
        if decision_df.empty:
            decision_df = sort_by_numeric(feasible_tmp, "TAC", ascending=True)

    evaluations = int(len(all_df))
    converged = int(bool_series(all_df["converged"]).sum()) if "converged" in all_df.columns else 0
    feasible_mask = bool_series(all_df["feasible"]) if "feasible" in all_df.columns else pd.Series([False] * len(all_df))
    feasible_df = all_df[feasible_mask] if not all_df.empty else pd.DataFrame()
    feasible = int(len(feasible_df))
    current_generation = int(pd.to_numeric(all_df["generation"], errors="coerce").max()) if "generation" in all_df.columns and not all_df.empty else None
    progress = evaluations / expected if expected else None
    min_tac_row = best_row(feasible_df, "TAC", True)
    decision_row = clean_record(decision_df.iloc[0].to_dict()) if not decision_df.empty else None
    best_recovery_row = best_row(feasible_df, "recovery_C3F8", False)
    best_energy_row = best_row(feasible_df, "energy_cost", True)

    ranges = get_ranges(settings)
    stdout, stderr = latest_log_pair(output_dir, workbook)
    processes = find_optimizer_processes(workbook)

    history = []
    if not history_df.empty:
        history_cols = [
            "generation",
            "evaluations",
            "population_feasible",
            "all_feasible",
            "pareto_count",
            "best_recovery_C3F8",
            "min_total_reboiler_duty",
            "min_TAC",
            "min_energy_cost",
            "min_solvent_loss_total",
        ]
        history = select_records(history_df, [c for c in history_cols if c in history_df.columns], limit=1000)
    paper_history = make_paper_history(all_df)

    return {
        "workbook": str(workbook),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checkpoint": {
            "path": str(default_checkpoint_path(workbook)),
            "used": checkpoint_used,
        },
        "summary": {
            "evaluations": evaluations,
            "expected_total": expected,
            "progress_fraction": progress,
            "complete": bool(expected and evaluations >= expected),
            "current_generation": current_generation,
            "total_generations": int(generations) if generations is not None else None,
            "converged": converged,
            "converged_rate": converged / evaluations if evaluations else None,
            "feasible": feasible,
            "feasible_rate": feasible / evaluations if evaluations else None,
            "pareto_count": int(len(pareto_df)),
            "min_TAC": clean_value(min_tac_row.get("TAC")) if min_tac_row else None,
            "eta_text": "ETA 待计算",
        },
        "process": {
            "running": bool(processes),
            "pids": [item["pid"] for item in processes],
            "commands": [item["command"] for item in processes],
        },
        "best": {
            "decision": decision_row,
            "min_TAC": min_tac_row,
            "max_recovery": best_recovery_row,
            "min_energy": best_energy_row,
        },
        "best_objectives": [clean_record(row) for row in make_best_objectives(feasible_df)],
        "decision_rank": select_records(decision_df, DECISION_COLUMNS, limit=12),
        "pareto": select_records(pareto_df, DECISION_COLUMNS, limit=200),
        "history": history,
        "paper_history": paper_history,
        "boundary_alerts": boundary_alerts(pareto_df, ranges),
        "logs": {
            "stdout_path": str(stdout) if stdout else None,
            "stderr_path": str(stderr) if stderr else None,
            "stdout_tail": read_tail(stdout),
            "stderr_tail": read_tail(stderr),
        },
        "settings": settings,
    }


class DashboardState:
    def __init__(self, workbook, token=None):
        self.workbook = Path(workbook)
        self.token = token
        self.cache = {}
        self.cache_time = {}
        self.cache_fingerprint = {}
        self.last_samples = {}


def status_fingerprint(workbook):
    workbook = Path(workbook)
    checkpoint = default_checkpoint_path(workbook)
    parts = []
    for path in (workbook, checkpoint):
        try:
            stat = path.stat()
            parts.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            parts.append((str(path), None, None))
    return tuple(parts)


def workbook_value(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return str(path)


def workbook_display_info(path):
    path = Path(path)
    settings = settings_dict(read_sheet(path, "settings"))
    solvent = str(settings.get("solvent") or "").strip()
    if solvent:
        label = solvent
    else:
        label = path.stem
    return {
        "path": path,
        "value": workbook_value(path),
        "label": label,
        "solvent": solvent or path.stem,
        "mtime": path.stat().st_mtime,
    }


def discover_workbooks(default_workbook):
    default_workbook = Path(default_workbook).resolve()
    output_dir = default_workbook.parent
    candidates = []
    for path in output_dir.glob("*.xlsx"):
        name = path.name.lower()
        if name.startswith("~$") or ".tmp." in name:
            continue
        candidates.append(path.resolve())
    if default_workbook.exists() and default_workbook not in candidates:
        candidates.append(default_workbook)

    latest_by_solvent = {}
    for path in candidates:
        try:
            info = workbook_display_info(path)
        except Exception:
            continue
        if not info["solvent"] or info["solvent"] == path.stem:
            continue
        key = info["solvent"]
        previous = latest_by_solvent.get(key)
        if previous is None or info["mtime"] > previous["mtime"]:
            latest_by_solvent[key] = info

    rows = sorted(
        latest_by_solvent.values(),
        key=lambda row: (row["solvent"].upper(), -row["mtime"]),
    )
    default_value = workbook_value(default_workbook)
    if default_workbook.exists() and all(row["value"] != default_value for row in rows):
        rows.append(workbook_display_info(default_workbook))
    return [
        {"value": row["value"], "label": row["label"], "solvent": row["solvent"]}
        for row in rows
    ]


def warm_workbook_cache(server):
    try:
        for item in discover_workbooks(server.state.workbook):
            workbook = Path(item["value"])
            if not workbook.is_absolute():
                workbook = BASE_DIR / workbook
            workbook = workbook.resolve()
            workbook_key = str(workbook)
            fingerprint = status_fingerprint(workbook)
            if server.state.cache_fingerprint.get(workbook_key) == fingerprint:
                continue
            payload = build_status(workbook)
            server.state.cache[workbook_key] = payload
            server.state.cache_time[workbook_key] = time.time()
            server.state.cache_fingerprint[workbook_key] = fingerprint
    except Exception:
        return


class Handler(BaseHTTPRequestHandler):
    server_version = "AspenMonitor/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            if not self.authorized(parsed):
                self.send_auth_page()
                return
            self.send_html(HTML)
            return
        if parsed.path == "/api/status":
            if not self.authorized(parsed):
                self.send_json({"error": "token missing or invalid"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self.send_status_json(parsed)
            return
        if parsed.path == "/api/workbooks":
            if not self.authorized(parsed):
                self.send_json({"error": "token missing or invalid"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self.send_workbooks_json()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, fmt, *args):
        return

    def send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_auth_page(self):
        html = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aspen 监控台访问验证</title>
<body style="font-family:Microsoft YaHei,Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0;padding:32px">
<main style="max-width:520px;margin:8vh auto;background:#fff;border:1px solid #dce3ef;border-radius:8px;padding:24px">
<h1 style="font-size:20px;margin:0 0 12px">需要访问 token</h1>
<p style="color:#657086">请使用带 token 的完整链接，或在下面输入 token。</p>
<form onsubmit="event.preventDefault(); const t=document.getElementById('t').value.trim(); if(t){ location.href='/?token='+encodeURIComponent(t); }">
<input id="t" type="password" autofocus style="width:100%;height:38px;border:1px solid #dce3ef;border-radius:8px;padding:0 10px">
<button style="margin-top:12px;height:38px;border:0;border-radius:8px;background:#0f766e;color:#fff;padding:0 16px">进入</button>
</form>
</main></body></html>"""
        self.send_html(html)

    def authorized(self, parsed):
        expected = self.server.state.token
        if not expected:
            return True
        query = parse_qs(parsed.query)
        supplied = None
        if "token" in query and query["token"]:
            supplied = query["token"][0]
        if not supplied:
            supplied = self.headers.get("X-Dashboard-Token")
        return bool(supplied and secrets.compare_digest(str(supplied), str(expected)))

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_workbooks_json(self):
        payload = {
            "default_workbook": workbook_value(self.server.state.workbook),
            "workbooks": discover_workbooks(self.server.state.workbook),
        }
        self.send_json(payload)

    def send_status_json(self, parsed):
        query = parse_qs(parsed.query)
        workbook = Path(query.get("workbook", [str(self.server.state.workbook)])[0])
        if not workbook.is_absolute():
            workbook = BASE_DIR / workbook
        workbook = workbook.resolve()
        workbook_key = str(workbook)
        try:
            fingerprint = status_fingerprint(workbook)
            cached = self.server.state.cache.get(workbook_key)
            if cached and self.server.state.cache_fingerprint.get(workbook_key) == fingerprint:
                payload = dict(cached)
                payload["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.send_json(payload)
                return

            payload = build_status(workbook)
            now = time.time()
            summary = payload.get("summary", {})
            evaluations = summary.get("evaluations")
            expected = summary.get("expected_total")
            previous = self.server.state.last_samples.get(workbook_key)
            if (
                isinstance(evaluations, int)
                and isinstance(expected, int)
                and evaluations < expected
                and previous is not None
            ):
                delta_rows = evaluations - previous["evaluations"]
                delta_time = now - previous["sample_time"]
                if delta_rows > 0 and delta_time > 0:
                    rate = delta_rows / delta_time
                    summary["eta_text"] = seconds_to_text((expected - evaluations) / rate)
            self.server.state.last_samples[workbook_key] = {
                "evaluations": evaluations,
                "sample_time": now,
            }
            self.server.state.cache[workbook_key] = payload
            self.server.state.cache_time[workbook_key] = now
            self.server.state.cache_fingerprint[workbook_key] = fingerprint
            self.send_json(payload)
        except Exception as exc:
            cached = self.server.state.cache.get(workbook_key)
            if cached:
                payload = dict(cached)
                payload["read_error"] = str(exc)
                payload["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.send_json(payload)
            else:
                self.send_json({"error": str(exc), "workbook": str(workbook)}, status=HTTPStatus.SERVICE_UNAVAILABLE)


def parse_args():
    parser = argparse.ArgumentParser(description="Serve a local Aspen optimization dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--workbook", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--token", default="", help="Optional access token required by web/API requests.")
    return parser.parse_args()


def main():
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.state = DashboardState(args.workbook, token=args.token or None)
    url = f"http://{args.host}:{args.port}/"
    print(f"Aspen optimization dashboard: {url}")
    print(f"Workbook: {Path(args.workbook).resolve()}")
    if args.token:
        print("Token authentication: enabled")
    threading.Thread(target=warm_workbook_cache, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping dashboard.")


if __name__ == "__main__":
    main()
