(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);

    const els = {
        app: $("app"),
        loginOverlay: $("login-overlay"),
        loginForm: $("login-form"),
        password: $("password"),
        loginError: $("login-error"),
        logoutBtn: $("logout-btn"),
        modelBadge: $("model-badge"),
        task: $("task"),
        problemWrap: $("problem-wrap"),
        problem: $("problem"),
        language: $("language"),
        code: $("code"),
        charCount: $("char-count"),
        status: $("status"),
        modeSwitch: $("mode-switch"),
        modeButtons: document.querySelectorAll("#mode-switch button"),
        reviewBtn: $("review-btn"),
        folderBtn: $("folder-btn"),
        folderInput: $("folder-input"),
        sampleBtn: $("sample-btn"),
        clearBtn: $("clear-btn"),
        copyBtn: $("copy-btn"),
        downloadBtn: $("download-btn"),
        fixBtn: $("fix-btn"),
        fixOverlay: $("fix-overlay"),
        fixCode: $("fix-code"),
        fixExplanation: $("fix-explanation"),
        fixCloseBtn: $("fix-close-btn"),
        fixCopyBtn: $("fix-copy-btn"),
        fixApplyBtn: $("fix-apply-btn"),
        feedbackBar: $("feedback-bar"),
        feedbackButtons: document.querySelectorAll(
            "#feedback-bar button[data-verdict]",
        ),
        historyBtn: $("history-btn"),
        historyOverlay: $("history-overlay"),
        historyCloseBtn: $("history-close-btn"),
        historyList: $("history-list"),
        historyDetail: $("history-detail"),
        historyStats: $("history-stats"),
        result: $("result"),
    };

    const state = {
        authRequired: false,
        maxChars: 20000,
        mode: "quick",
        task: "review",
        loading: false,
        fixing: false,
        folderLoading: false,
        reviewText: "",
        fixText: "",
        reviewId: null,
    };

    const EMPTY_STATE = `
        <div class="empty-state">
            <div class="empty-icon" aria-hidden="true">⌁</div>
            <p>审查结果会显示在这里</p>
            <span>快速模式先给结论和 Top 3 问题，需要完整报告可切换到“深度”</span>
            <span>支持 Python、Java、JavaScript、Go、SQL 等语言</span>
        </div>`;

    const SAMPLE_CODE = `def get_user(username):
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return db.execute(query)`;

    async function api(path, options = {}) {
        const response = await fetch(path, {
            credentials: "same-origin",
            ...options,
            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {}),
            },
        });

        const text = await response.text();
        let data = null;
        if (text) {
            try {
                data = JSON.parse(text);
            } catch (error) {
                data = null;
            }
        }

        if (!response.ok) {
            const requestError = new Error(formatError(data, response.status));
            requestError.status = response.status;
            throw requestError;
        }

        return data;
    }

    function formatError(data, status) {
        if (data && typeof data.detail === "string") {
            return data.detail;
        }
        if (data && Array.isArray(data.detail)) {
            return data.detail
                .map((item) => {
                    const location = Array.isArray(item.loc)
                        ? item.loc.filter((part) => part !== "body").join(".")
                        : "";
                    return `${location ? location + "：" : ""}${item.msg || "参数错误"}`;
                })
                .join("\n");
        }
        return `请求失败（HTTP ${status}）`;
    }

    function showLogin(message = "") {
        els.app.hidden = true;
        els.loginOverlay.hidden = false;
        if (message) {
            els.loginError.textContent = message;
            els.loginError.hidden = false;
        } else {
            els.loginError.hidden = true;
        }
        window.setTimeout(() => els.password.focus(), 60);
    }

    function showApp() {
        els.loginOverlay.hidden = true;
        els.app.hidden = false;
        els.logoutBtn.hidden = !state.authRequired;
    }

    function setStatus(text, type = "") {
        els.status.textContent = text;
        els.status.className = "status" + (type ? ` ${type}` : "");
    }

    function syncTaskUI() {
        state.task = els.task.value;
        const isProblem = state.task === "problem";
        els.problemWrap.hidden = !isProblem;
        els.modeSwitch.hidden = state.task !== "review";

        if (isProblem) {
            els.code.placeholder = "粘贴你的题解代码……";
            setStatus("题目模式：先粘贴题目描述，再粘贴代码", "");
        } else if (state.task === "test_framework") {
            els.code.placeholder = "粘贴自动化测试框架/脚本代码……";
            setStatus("测试框架模式：检查安全、边界和稳定性", "");
        } else {
            els.code.placeholder = "在这里粘贴需要审查的代码……";
            setStatus("已切换到普通审查", "");
        }
    }

    function updateReviewButton() {
        const length = els.code.value.length;
        els.reviewBtn.disabled =
            state.loading || length === 0 || length > state.maxChars;
    }

    function updateCount() {
        const length = els.code.value.length;
        els.charCount.textContent =
            `${length.toLocaleString()} / ${state.maxChars.toLocaleString()}`;
        els.charCount.classList.toggle("over", length > state.maxChars);
        updateReviewButton();
    }

    function setLoading(loading) {
        state.loading = loading;
        els.folderBtn.disabled = loading;
        els.reviewBtn.classList.toggle("loading", loading);
        let loadingText =
            state.mode === "quick" ? "快速审查中…" : "深度审查中…";
        if (state.task === "problem") {
            loadingText = "题目评测中…";
        } else if (state.task === "test_framework") {
            loadingText = "测试框架审查中…";
        }
        els.reviewBtn.innerHTML = loading
            ? `<span class="spinner"></span> ${loadingText}`
            : '开始审查 <span class="shortcut">Ctrl ↵</span>';
        updateReviewButton();
    }

    function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, (character) => {
            const entities = {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            };
            return entities[character];
        });
    }

    function extractCodeBlock(text) {
        const match = text.match(/```[^\n]*\r?\n([\s\S]*?)```/);

        if (match) {
            return {
                code: match[1].replace(/\s+$/, ""),
                explanation: text.replace(match[0], "").trim(),
            };
        }

        return {
            code: text.trim(),
            explanation: "",
        };
    }

    function renderMarkdown(text) {
        if (!text) {
            els.result.innerHTML =
                '<div class="empty-state"><p>模型没有返回内容</p></div>';
            return;
        }

        try {
            if (window.marked && window.DOMPurify) {
                els.result.innerHTML = window.DOMPurify.sanitize(
                    window.marked.parse(text),
                );
            } else {
                els.result.innerHTML =
                    `<pre class="fallback-markdown">${escapeHtml(text)}</pre>`;
            }
        } catch (error) {
            els.result.innerHTML =
                `<pre class="fallback-markdown">${escapeHtml(text)}</pre>`;
        }

        els.result.scrollTop = 0;
    }

    function renderMarkdownInto(element, text) {
        if (!text) {
            element.textContent = "（没有内容）";
            return;
        }

        try {
            if (window.marked && window.DOMPurify) {
                element.innerHTML = window.DOMPurify.sanitize(
                    window.marked.parse(text),
                );
            } else {
                element.textContent = text;
            }
        } catch (error) {
            element.textContent = text;
        }
    }

    async function copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return;
        }

        const helper = document.createElement("textarea");
        helper.value = text;
        helper.setAttribute("readonly", "");
        helper.style.position = "fixed";
        helper.style.top = "-1000px";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        const copied = document.execCommand("copy");
        helper.remove();
        if (!copied) {
            throw new Error("copy failed");
        }
    }

    function parseSseEvent(block) {
        const data = block
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim())
            .join("\n");

        if (!data) {
            return null;
        }

        try {
            return JSON.parse(data);
        } catch (error) {
            return null;
        }
    }

    function renderStreamingPreview(text) {
        let preview = document.getElementById("stream-preview");
        if (!preview) {
            els.result.innerHTML =
                '<pre id="stream-preview" class="streaming-preview" aria-live="polite"></pre>';
            preview = document.getElementById("stream-preview");
        }
        preview.textContent = text;
        els.result.scrollTop = els.result.scrollHeight;
    }

    async function streamReview(code) {
        const response = await fetch("/api/review/stream", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                code_snippet: code,
                language: els.language.value,
                mode: state.mode,
                task: state.task,
                problem_statement: els.problem.value,
            }),
        });

        if (response.status === 401) {
            const error = new Error("登录已过期，请重新登录");
            error.status = 401;
            throw error;
        }

        if (!response.ok) {
            let data = null;
            try {
                data = await response.json();
            } catch (error) {
                data = null;
            }
            const requestError = new Error(formatError(data, response.status));
            requestError.status = response.status;
            throw requestError;
        }

        const contentType = response.headers.get("content-type") || "";
        if (!contentType.includes("text/event-stream")) {
            const data = await response.json();
            state.reviewText =
                data && typeof data.review_result === "string"
                    ? data.review_result
                    : "";
            renderMarkdown(state.reviewText);
            return true;
        }

        if (!response.body) {
            return false;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let text = "";
        let reviewId = null;
        let streamError = null;

        els.result.innerHTML =
            '<pre id="stream-preview" class="streaming-preview" aria-live="polite"></pre>';

        while (!streamError) {
            const { value, done } = await reader.read();
            if (done) {
                break;
            }

            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split("\n\n");
            buffer = blocks.pop() || "";

            for (const block of blocks) {
                const event = parseSseEvent(block);
                if (!event) {
                    continue;
                }

                if (event.type === "delta" && typeof event.content === "string") {
                    text += event.content;
                    renderStreamingPreview(text);
                } else if (event.type === "done") {
                    if (event.review_id) {
                        reviewId = event.review_id;
                    }
                } else if (event.type === "error") {
                    streamError = new Error(event.message || "AI 模型调用失败");
                    break;
                }
            }
        }

        buffer += decoder.decode();

        if (buffer.trim()) {
            const event = parseSseEvent(buffer);
            if (event && event.type === "delta" && typeof event.content === "string") {
                text += event.content;
            } else if (event && event.type === "done" && event.review_id) {
                reviewId = event.review_id;
            } else if (event && event.type === "error") {
                streamError = new Error(event.message || "AI 模型调用失败");
            }
        }

        state.reviewText = text;
        state.reviewId = reviewId;

        if (streamError) {
            if (text) {
                renderMarkdown(text);
            }
            throw streamError;
        }

        if (!text) {
            throw new Error("AI 模型没有返回内容");
        }

        renderMarkdown(text);
        return true;
    }

    async function runReview() {
        const code = els.code.value;

        if (!code.trim()) {
            setStatus("请先粘贴需要审查的代码", "error");
            els.code.focus();
            return;
        }

        if (code.length > state.maxChars) {
            setStatus(
                `代码过长，最多 ${state.maxChars.toLocaleString()} 个字符`,
                "error",
            );
            return;
        }

        if (state.task === "problem" && !els.problem.value.trim()) {
            setStatus("题目模式需要先粘贴题目描述", "error");
            els.problem.focus();
            return;
        }

        if (state.loading || state.folderLoading) {
            return;
        }

        state.reviewText = "";
        state.reviewId = null;
        els.feedbackBar.hidden = true;
        setLoading(true);

        let runningText =
            state.mode === "quick" ? "正在快速审查……" : "正在深度审查……";
        if (state.task === "problem") {
            runningText = "正在评测题目……";
        } else if (state.task === "test_framework") {
            runningText = "正在审查测试框架……";
        }
        setStatus(runningText, "loading");
        els.copyBtn.disabled = true;
        els.downloadBtn.disabled = true;
        els.fixBtn.disabled = true;
        const startedAt = Date.now();

        try {
            const streamed = await streamReview(code);

            if (!streamed) {
                const data = await api("/api/review", {
                    method: "POST",
                    body: JSON.stringify({
                        code_snippet: code,
                        language: els.language.value,
                        mode: state.mode,
                        task: state.task,
                        problem_statement: els.problem.value,
                    }),
                });
                state.reviewText =
                    data && typeof data.review_result === "string"
                        ? data.review_result
                        : "";
                state.reviewId =
                    data && data.review_id ? data.review_id : null;
                renderMarkdown(state.reviewText);
            }

            if (!state.reviewText) {
                throw new Error("AI 模型没有返回内容");
            }

            els.feedbackBar.hidden = !state.reviewId;

            const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
            let modeLabel =
                state.mode === "quick" ? "快速审查完成" : "深度审查完成";
            if (state.task === "problem") {
                modeLabel = "题目评测完成";
            } else if (state.task === "test_framework") {
                modeLabel = "测试框架审查完成";
            }
            setStatus(`${modeLabel}，用时 ${seconds} 秒`, "ok");
        } catch (error) {
            if (error.status === 401) {
                showLogin("登录已过期，请重新登录");
                return;
            }
            setStatus(error.message || "审查失败", "error");
        } finally {
            setLoading(false);
            els.copyBtn.disabled = !state.reviewText;
            els.downloadBtn.disabled = !state.reviewText;
            els.fixBtn.disabled = !state.reviewText;
            els.feedbackBar.hidden = !state.reviewId;
        }
    }

    async function runFix() {
        const code = els.code.value;

        if (!code.trim()) {
            setStatus("请先粘贴需要修复的代码", "error");
            return;
        }

        if (!state.reviewText) {
            setStatus("请先完成一次审查，再生成修复建议", "error");
            return;
        }

        if (state.loading || state.fixing || state.folderLoading) {
            return;
        }

        state.fixing = true;
        els.fixBtn.disabled = true;
        els.fixBtn.textContent = "修复中…";
        setStatus("正在生成修复代码……", "loading");

        try {
            const data = await api("/api/review", {
                method: "POST",
                body: JSON.stringify({
                    code_snippet: code,
                    language: els.language.value,
                    mode: "fix",
                    review_result: state.reviewText.slice(0, 6000),
                }),
            });

            const result =
                data && typeof data.review_result === "string"
                    ? data.review_result
                    : "";
            if (!result.trim()) {
                throw new Error("模型没有返回修复代码");
            }

            const parsed = extractCodeBlock(result);
            state.fixText = parsed.code;
            els.fixCode.textContent = parsed.code;
            els.fixExplanation.textContent =
                parsed.explanation || "未提供额外说明。";
            els.fixOverlay.hidden = false;
            setStatus("修复建议已生成，请人工确认", "ok");
        } catch (error) {
            if (error.status === 401) {
                showLogin("登录已过期，请重新登录");
                return;
            }
            setStatus(error.message || "生成修复代码失败", "error");
        } finally {
            state.fixing = false;
            els.fixBtn.disabled = !state.reviewText;
            els.fixBtn.textContent = "修复代码";
        }
    }

    function verdictLabel(value) {
        const labels = {
            helpful: "有用",
            false_positive: "误报",
            fixed: "已修复",
        };
        return labels[value] || value;
    }

    function formatTime(value) {
        if (!value) {
            return "";
        }
        const date = new Date(value.replace(" ", "T") + "Z");
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    async function sendFeedback(verdict) {
        if (!state.reviewId) {
            return;
        }
        try {
            await api(`/api/reviews/${state.reviewId}/feedback`, {
                method: "POST",
                body: JSON.stringify({ verdict }),
            });
            setStatus("反馈已记录，会用于后续审查", "ok");
            els.feedbackBar.hidden = true;
        } catch (error) {
            if (error.status === 401) {
                showLogin("登录已过期，请重新登录");
                return;
            }
            setStatus(error.message || "反馈失败", "error");
        }
    }

    function renderHistory(items, insights) {
        els.historyList.innerHTML = "";

        if (!items.length) {
            const empty = document.createElement("p");
            empty.className = "history-empty";
            empty.textContent = "还没有审查记录";
            els.historyList.appendChild(empty);
        } else {
            items.forEach((item) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "history-item";

                const title = document.createElement("strong");
                title.textContent = item.summary || `审查 #${item.id}`;

                const meta = document.createElement("span");
                meta.textContent =
                    `${formatTime(item.created_at)} · ${item.language} · ${item.mode}`;

                button.append(title, meta);

                if (item.latest_verdict) {
                    const badge = document.createElement("span");
                    badge.textContent =
                        `反馈：${verdictLabel(item.latest_verdict)}`;
                    button.appendChild(badge);
                }

                button.addEventListener("click", () => {
                    loadHistoryDetail(item.id);
                });
                els.historyList.appendChild(button);
            });
        }

        const feedback = insights.feedback || {};
        els.historyStats.textContent =
            `共 ${insights.total_reviews || 0} 次审查 · ` +
            `最近 7 天 ${insights.reviews_last_7_days || 0} 次 · ` +
            `有用 ${feedback.helpful || 0} / ` +
            `误报 ${feedback.false_positive || 0} / ` +
            `已修复 ${feedback.fixed || 0}`;
    }

    async function loadHistoryDetail(reviewId) {
        els.historyDetail.textContent = "加载中…";
        try {
            const data = await api(`/api/reviews/${reviewId}`);
            if (data.review_text) {
                renderMarkdownInto(els.historyDetail, data.review_text);
            } else {
                els.historyDetail.textContent = "（这条记录没有审查内容）";
            }
        } catch (error) {
            if (error.status === 401) {
                els.historyOverlay.hidden = true;
                showLogin("登录已过期，请重新登录");
                return;
            }
            els.historyDetail.textContent =
                error.message || "加载审查详情失败";
        }
    }

    async function openHistory() {
        els.historyOverlay.hidden = false;
        els.historyStats.textContent = "加载中…";
        els.historyDetail.textContent = "选择一条记录查看详情";

        try {
            const [list, insights] = await Promise.all([
                api("/api/reviews?limit=20"),
                api("/api/insights"),
            ]);
            renderHistory(list.items || [], insights || {});
        } catch (error) {
            if (error.status === 401) {
                els.historyOverlay.hidden = true;
                showLogin("登录已过期，请重新登录");
                return;
            }
            els.historyStats.textContent =
                error.message || "加载审查历史失败";
        }
    }

    async function runFolderAnalysis(file) {
        if (!file || state.loading || state.fixing || state.folderLoading) {
            return;
        }

        if (!file.name.toLowerCase().endsWith(".zip")) {
            setStatus("请选择 .zip 文件", "error");
            return;
        }

        state.folderLoading = true;
        state.reviewId = null;
        state.reviewText = "";
        els.feedbackBar.hidden = true;
        els.fixBtn.disabled = true;
        els.copyBtn.disabled = true;
        els.downloadBtn.disabled = true;
        els.folderBtn.disabled = true;
        els.folderBtn.textContent = "分析中…";
        setStatus("正在解析文件夹并抽样审查高风险文件……", "loading");

        const form = new FormData();
        form.append("file", file);

        try {
            const response = await fetch("/api/analyze-folder", {
                method: "POST",
                credentials: "same-origin",
                body: form,
            });

            let data = null;
            try {
                data = await response.json();
            } catch (error) {
                data = null;
            }

            if (!response.ok) {
                const requestError = new Error(
                    formatError(data, response.status),
                );
                requestError.status = response.status;
                throw requestError;
            }

            state.reviewText = data.report || "";
            renderMarkdown(state.reviewText);

            const summary = data.summary || {};
            setStatus(
                `文件夹分析完成：审查 ${summary.bundles || 0} 个包 / ` +
                    `${summary.selected_files || 0} 个文件，` +
                    `跳过敏感文件 ${summary.sensitive_files || 0} 个`,
                "ok",
            );
        } catch (error) {
            if (error.status === 401) {
                showLogin("登录已过期，请重新登录");
                return;
            }
            setStatus(error.message || "文件夹分析失败", "error");
        } finally {
            state.folderLoading = false;
            els.folderBtn.disabled = false;
            els.folderBtn.textContent = "文件夹";
            els.copyBtn.disabled = !state.reviewText;
            els.downloadBtn.disabled = !state.reviewText;
            els.fixBtn.disabled = true;
            els.folderInput.value = "";
        }
    }

    async function init() {
        try {
            const [meResponse, configResponse] = await Promise.all([
                fetch("/api/me", { credentials: "same-origin" }),
                fetch("/api/config", { credentials: "same-origin" }),
            ]);

            if (!meResponse.ok) {
                throw new Error(`HTTP ${meResponse.status}`);
            }

            const me = await meResponse.json();
            const config = configResponse.ok ? await configResponse.json() : {};

            state.authRequired = Boolean(me.auth_required);
            state.maxChars = Number(config.max_code_chars) || 20000;
            els.modelBadge.textContent = config.model || "AI";

            if (state.authRequired && !me.authenticated) {
                showLogin();
            } else {
                showApp();
            }

            updateCount();
        } catch (error) {
            showApp();
            setStatus("无法连接服务，请确认后端已启动", "error");
        }
    }

    els.loginForm.addEventListener("submit", async (event) => {
        event.preventDefault();

        const password = els.password.value;
        if (!password) {
            return;
        }

        const submitButton = els.loginForm.querySelector(
            'button[type="submit"]',
        );
        submitButton.disabled = true;
        submitButton.textContent = "验证中…";
        els.loginError.hidden = true;

        try {
            await api("/api/login", {
                method: "POST",
                body: JSON.stringify({ password }),
            });
            els.password.value = "";
            showApp();
            setStatus("登录成功", "ok");
            els.code.focus();
        } catch (error) {
            els.loginError.textContent = error.message || "登录失败";
            els.loginError.hidden = false;
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = "进入";
        }
    });

    els.logoutBtn.addEventListener("click", async () => {
        try {
            await api("/api/logout", { method: "POST" });
        } catch (error) {
            // 即使请求失败，也回到登录页
        }
        state.reviewText = "";
        state.reviewId = null;
        state.fixText = "";
        els.copyBtn.disabled = true;
        els.downloadBtn.disabled = true;
        els.fixBtn.disabled = true;
        els.fixOverlay.hidden = true;
        els.feedbackBar.hidden = true;
        els.historyOverlay.hidden = true;
        showLogin("已退出，请重新登录");
    });

    els.task.addEventListener("change", syncTaskUI);

    els.reviewBtn.addEventListener("click", runReview);

    els.modeButtons.forEach((button) => {
        button.addEventListener("click", () => {
            if (state.loading) {
                return;
            }

            state.mode = button.dataset.mode;
            els.modeButtons.forEach((item) => {
                const active = item === button;
                item.classList.toggle("active", active);
                item.setAttribute("aria-pressed", String(active));
            });

            setStatus(
                state.mode === "quick"
                    ? "已切换到快速审查：只列结论和 Top 3 问题"
                    : "已切换到深度审查：输出完整报告",
                "",
            );
        });
    });

    els.folderBtn.addEventListener("click", () => {
        els.folderInput.click();
    });

    els.folderInput.addEventListener("change", () => {
        const file = els.folderInput.files && els.folderInput.files[0];
        if (file) {
            runFolderAnalysis(file);
        }
    });

    els.sampleBtn.addEventListener("click", () => {
        els.code.value = SAMPLE_CODE;
        els.language.value = "python";
        updateCount();
        setStatus("已载入示例代码", "");
        els.code.focus();
    });

    els.clearBtn.addEventListener("click", () => {
        els.code.value = "";
        els.problem.value = "";
        state.reviewText = "";
        state.reviewId = null;
        state.fixText = "";
        els.result.innerHTML = EMPTY_STATE;
        els.copyBtn.disabled = true;
        els.downloadBtn.disabled = true;
        els.fixBtn.disabled = true;
        els.fixOverlay.hidden = true;
        els.feedbackBar.hidden = true;
        els.folderInput.value = "";
        updateCount();
        setStatus("已清空", "");
        els.code.focus();
    });

    els.fixBtn.addEventListener("click", runFix);

    els.fixCloseBtn.addEventListener("click", () => {
        els.fixOverlay.hidden = true;
    });

    els.fixOverlay.addEventListener("click", (event) => {
        if (event.target === els.fixOverlay) {
            els.fixOverlay.hidden = true;
        }
    });

    els.fixCopyBtn.addEventListener("click", async () => {
        if (!state.fixText) {
            return;
        }
        try {
            await copyText(state.fixText);
            setStatus("修复代码已复制", "ok");
        } catch (error) {
            setStatus("复制失败，请手动选择文本", "error");
        }
    });

    els.fixApplyBtn.addEventListener("click", () => {
        if (!state.fixText) {
            return;
        }

        els.code.value = state.fixText;
        state.reviewText = "";
        state.reviewId = null;
        state.fixText = "";
        els.result.innerHTML = EMPTY_STATE;
        els.copyBtn.disabled = true;
        els.downloadBtn.disabled = true;
        els.fixBtn.disabled = true;
        els.fixOverlay.hidden = true;
        els.feedbackBar.hidden = true;
        updateCount();
        setStatus("已替换编辑器内容，请重新审查确认", "ok");
        els.code.focus();
    });

    els.feedbackButtons.forEach((button) => {
        button.addEventListener("click", () => {
            sendFeedback(button.dataset.verdict);
        });
    });

    els.historyBtn.addEventListener("click", openHistory);

    els.historyCloseBtn.addEventListener("click", () => {
        els.historyOverlay.hidden = true;
    });

    els.historyOverlay.addEventListener("click", (event) => {
        if (event.target === els.historyOverlay) {
            els.historyOverlay.hidden = true;
        }
    });

    els.copyBtn.addEventListener("click", async () => {
        if (!state.reviewText) {
            return;
        }
        try {
            await copyText(state.reviewText);
            setStatus("已复制到剪贴板", "ok");
        } catch (error) {
            setStatus("复制失败，请手动选择文本", "error");
        }
    });

    els.downloadBtn.addEventListener("click", () => {
        if (!state.reviewText) {
            return;
        }

        const blob = new Blob([state.reviewText], {
            type: "text/markdown;charset=utf-8",
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const stamp = new Date()
            .toISOString()
            .slice(0, 19)
            .replace(/[:T]/g, "-");

        link.href = url;
        link.download = `code-review-${stamp}.md`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        setStatus("已开始下载", "ok");
    });

    els.code.addEventListener("input", updateCount);

    els.code.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
            event.preventDefault();
            runReview();
            return;
        }

        if (event.key === "Tab") {
            event.preventDefault();
            const start = els.code.selectionStart;
            const end = els.code.selectionEnd;
            els.code.value =
                els.code.value.slice(0, start) +
                "    " +
                els.code.value.slice(end);
            els.code.selectionStart = start + 4;
            els.code.selectionEnd = start + 4;
            updateCount();
        }
    });

    init();
})();
