// -*- coding: utf-8 -*-
// 检验科试剂管理系统 - 通用交互脚本
(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        // 只给写操作（POST）表单注入 CSRF token；GET 搜索/翻页表单若也注入会让 URL 变脏
        const csrfMeta = document.querySelector("meta[name='csrf-token']");
        if (csrfMeta) {
            document.querySelectorAll("form").forEach(function (form) {
                const method = (form.getAttribute("method") || "get").toLowerCase();
                if (method !== "post") { return; }
                if (!form.querySelector("input[name='csrf_token']")) {
                    const input = document.createElement("input");
                    input.type = "hidden";
                    input.name = "csrf_token";
                    input.value = csrfMeta.content;
                    form.appendChild(input);
                }
            });
        }
        const body = document.body;
        const toggle = document.getElementById("sidebarToggle");

        // ---------- 侧边栏折叠 / 移动开关 ----------
        if (window.innerWidth > 768 && localStorage.getItem("sidebar_collapsed") === "1") {
            body.classList.add("sidebar-collapsed");
        }
        if (toggle) {
            toggle.addEventListener("click", function () {
                if (window.innerWidth <= 768) {
                    body.classList.toggle("sidebar-open");
                } else {
                    body.classList.toggle("sidebar-collapsed");
                    localStorage.setItem(
                        "sidebar_collapsed",
                        body.classList.contains("sidebar-collapsed") ? "1" : "0"
                    );
                }
            });
        }

        // ---------- 提示信息自动关闭 ----------
        setTimeout(function () {
            document.querySelectorAll(".alert.auto-dismiss").forEach(function (el) {
                el.classList.remove("show");
                setTimeout(function () { el.remove(); }, 300);
            });
        }, 4500);

        // ---------- 筛选下拉框：按最长选项自动加宽，名称过长时悬停看全名 ----------
        // 用真实渲染的隐藏 span 量文字宽度，比 canvas 估算准，避免最后一个字被下拉箭头压住
        const measureEl = document.createElement("span");
        measureEl.style.cssText = "position:absolute;left:-9999px;top:-9999px;white-space:nowrap;visibility:hidden";
        document.body.appendChild(measureEl);
        function fitFilterSelect(sel) {
            const cs = window.getComputedStyle(sel);
            measureEl.style.fontSize = cs.fontSize;
            measureEl.style.fontFamily = cs.fontFamily;
            measureEl.style.fontWeight = cs.fontWeight;
            let maxW = 0;
            Array.from(sel.options).forEach(function (opt) {
                if (opt.hidden) { return; }
                measureEl.textContent = opt.text;
                const w = measureEl.getBoundingClientRect().width;
                if (w > maxW) { maxW = w; }
            });
            // 72px：左右内边距 + 下拉箭头 + 让末尾字不贴箭头的余量；手机上不超过筛选条宽度
            const bar = sel.closest(".search-bar");
            const capW = bar ? bar.clientWidth - 44 : 500;
            sel.style.minWidth = Math.max(110, Math.min(500, capW, Math.ceil(maxW + 72))) + "px";
            const opt = sel.options[sel.selectedIndex];
            sel.title = (sel.value && opt) ? opt.text : "";
        }
        document.querySelectorAll(".search-bar select.form-select").forEach(function (sel) {
            fitFilterSelect(sel);
            sel.addEventListener("change", function () { fitFilterSelect(sel); });
        });

        // ---------- 启用所有 tooltip ----------
        if (window.bootstrap) {
            document.querySelectorAll("[data-bs-toggle='tooltip']").forEach(function (el) {
                new bootstrap.Tooltip(el);
            });
        }
    });

    // ---------- 通用危险操作确认弹窗（删除等） ----------
    window.confirmAction = function (url, id, text, idField) {
        const form = document.getElementById("confirmForm");
        const idInput = document.getElementById("confirmId");
        const textEl = document.getElementById("confirmText");
        if (!form) { if (window.confirm(text)) { /* fallback */ } return; }
        form.action = url;
        idInput.name = idField || "id";
        idInput.value = id;
        textEl.textContent = text || "确定要执行此操作吗？此操作不可撤销。";
        new bootstrap.Modal(document.getElementById("confirmModal")).show();
    };
})();
