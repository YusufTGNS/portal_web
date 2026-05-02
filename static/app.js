const THEME_KEY = "theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  if (theme === "light") {
    toggle.textContent = "☀️ Açık";
    toggle.setAttribute("aria-label", "Açık tema aktif");
  } else {
    toggle.textContent = "🌙 Koyu";
    toggle.setAttribute("aria-label", "Koyu tema aktif");
  }
}

function resolveInitialTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

applyTheme(resolveInitialTheme());

const themeToggle = document.getElementById("theme-toggle");
if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    const next = current === "dark" ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
}

function setVisibility(input, visible) {
  if (!input) return;
  input.type = visible ? "text" : "password";
}

document.querySelectorAll(".reveal-btn").forEach((btn) => {
  const targetId = btn.dataset.target;
  const input = document.getElementById(targetId);
  if (!input) return;

  let visible = false;
  btn.addEventListener("click", () => {
    visible = !visible;
    setVisibility(input, visible);
    btn.textContent = visible ? "🙉 Gizle" : "🙈 Göster";
  });
});

document.addEventListener("submit", (ev) => {
  const form = ev.target.closest("form[data-confirm-message]");
  if (!form) return;
  const message = form.dataset.confirmMessage || "Bu işlem onay gerektiriyor. Devam edilsin mi?";
  if (!window.confirm(message)) {
    ev.preventDefault();
  }
});

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function initPortalRealtime() {
  const root = document.getElementById("portal-realtime");
  if (!root || typeof io === "undefined") return;

  const userId = Number(root.dataset.userId || 0);
  const csrfToken = root.dataset.csrfToken;
  const deletedText = root.dataset.deletedText || "Mesaj silindi";
  const deleteUrl = (id) => root.dataset.deleteUrlTemplate.replace("__id__", id);
  const downloadUrl = (id) => root.dataset.downloadUrlTemplate.replace("__id__", id);

  const socket = io();
  const threadList = document.getElementById("thread-list");
  const unreadCountEl = document.getElementById("unread-count");
  const recipientSelect = document.getElementById("recipient-select");
  const dmBody = document.getElementById("dm-body");
  const typingDmEl = document.getElementById("typing-indicator-dm");
  const onlineUsersEl = document.getElementById("online-users");
  const dmForm = document.getElementById("dm-form");

  let onlineUserIds = new Set();
  let dmTypingTimer = null;

  function updateOnlineUi() {
    const contacts = Array.from(document.querySelectorAll("[data-contact-id]"));
    let onlineCount = 0;
    contacts.forEach((el) => {
      const uid = Number(el.dataset.contactId || 0);
      const isOnline = onlineUserIds.has(uid);
      if (isOnline) onlineCount += 1;
      const strong = el.querySelector("strong");
      if (strong) {
        const name = strong.textContent.replace(/\s*[🟢⚪]$/, "");
        strong.textContent = `${name} ${isOnline ? "🟢" : "⚪"}`;
      }
    });
    if (onlineUsersEl) {
      onlineUsersEl.textContent = `Online: ${onlineCount} / ${contacts.length}`;
    }
  }

  function appendThread(message) {
    if (!threadList) return;
    const isMine = Number(message.sender_id) === userId;
    const isDeleted = String(message.body || "").trim() === deletedText;
    const html = `
      <div class="stack-item ${isMine ? "my-msg" : "peer-msg"}" data-message-id="${message.id}">
        <div class="message-head">
          <strong>${escapeHtml(message.sender_username)}</strong>
          <span class="muted">${escapeHtml(message.created_at)}</span>
        </div>
        <p class="muted">${escapeHtml(message.body)}</p>
        ${message.has_attachment && !isDeleted ? `<a class="btn btn-outline" href="${downloadUrl(message.id)}">Dosya Eki</a>` : ""}
        ${isMine && !isDeleted ? `<button class="btn btn-danger js-delete-message" type="button" data-message-id="${message.id}" data-confirm-message="Bu mesaj silinecek. Devam edilsin mi?">Sil</button>` : ""}
      </div>
    `;
    threadList.insertAdjacentHTML("beforeend", html);
    threadList.scrollTop = threadList.scrollHeight;
  }

  function markMessageDeleted(messageId, text) {
    const cards = document.querySelectorAll(`[data-message-id="${messageId}"]`);
    cards.forEach((card) => {
      const bodyEl = card.querySelector("p.muted");
      if (bodyEl) bodyEl.textContent = text;
      card.querySelectorAll("a.btn.btn-outline").forEach((a) => a.remove());
      const delBtn = card.querySelector(".js-delete-message");
      if (delBtn) delBtn.remove();
    });
  }

  socket.on("connect", () => {});

  socket.on("presence_snapshot", (data) => {
    onlineUserIds = new Set(data.online_user_ids || []);
    updateOnlineUi();
  });

  socket.on("user_presence", (data) => {
    if (!data || typeof data.user_id === "undefined") return;
    if (data.online) onlineUserIds.add(Number(data.user_id));
    else onlineUserIds.delete(Number(data.user_id));
    updateOnlineUi();
  });

  socket.on("unread_count", (data) => {
    if (unreadCountEl && data && typeof data.count !== "undefined") {
      unreadCountEl.textContent = data.count;
    }
  });

  socket.on("typing_dm", (data) => {
    if (!typingDmEl || !data || !data.is_typing) {
      if (typingDmEl) typingDmEl.textContent = "";
      return;
    }
    typingDmEl.textContent = `${data.from_username} yazıyor...`;
    window.clearTimeout(dmTypingTimer);
    dmTypingTimer = window.setTimeout(() => {
      typingDmEl.textContent = "";
    }, 1500);
  });

  socket.on("dm_message", (message) => {
    if (!message) return;
    const peerId = Number(recipientSelect ? recipientSelect.value || 0 : 0);
    const fromMe = Number(message.sender_id) === userId;
    const inCurrentThread =
      (fromMe && Number(message.recipient_id) === peerId) ||
      (!fromMe && Number(message.sender_id) === peerId);
    if (inCurrentThread) {
      appendThread({ ...message, has_attachment: !!message.has_attachment });
    }
  });

  socket.on("dm_message_deleted", (data) => {
    if (!data) return;
    markMessageDeleted(data.message_id, data.body || deletedText);
  });

  if (dmBody && recipientSelect) {
    dmBody.addEventListener("input", () => {
      const rid = Number(recipientSelect.value || 0);
      if (!rid) return;
      socket.emit("typing_dm", { recipient_id: rid, is_typing: dmBody.value.length > 0 });
    });
  }

  if (dmForm) {
    dmForm.addEventListener("submit", (ev) => {
      const fileInput = dmForm.querySelector('input[name="attachment"]');
      const hasFile = fileInput && fileInput.files && fileInput.files.length > 0;
      if (hasFile) return;
      const recipientId = Number(recipientSelect.value || 0);
      const subject = "DM";
      const body = (dmForm.querySelector('textarea[name="body"]') || {}).value || "";
      if (!recipientId || !body.trim()) return;
      ev.preventDefault();
      socket.emit("send_dm", {
        recipient_id: recipientId,
        subject: subject.trim(),
        body: body.trim(),
      });
      const attachmentInput = dmForm.querySelector('input[name="attachment"]');
      if (attachmentInput) attachmentInput.value = "";
      dmBody.value = "";
    });
  }

  if (threadList) {
    threadList.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".js-delete-message");
      if (!btn) return;
      const messageId = Number(btn.dataset.messageId || 0);
      if (!messageId) return;
      const confirmMessage = btn.dataset.confirmMessage || "Bu mesaj silinecek. Devam edilsin mi?";
      if (!window.confirm(confirmMessage)) return;
      try {
        const response = await fetch(deleteUrl(messageId), {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          },
          body: `csrf_token=${encodeURIComponent(csrfToken)}`,
        });
        if (!response.ok) return;
      } catch (_) {
        // silent fail to avoid breaking chat UX
      }
    });
  }
}

initPortalRealtime();
