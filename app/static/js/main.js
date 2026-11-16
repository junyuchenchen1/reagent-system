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
