"use strict";
const byId = (id) => document.getElementById(id);
const teacher = document.querySelector("main").dataset.view === "teacher";
const message = byId("message");
const formatTime = (value) => new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", dateStyle: "short", timeStyle: "medium"
}).format(new Date(value));

async function request(path, data, headers = {}) {
  const options = data === undefined ? {} : { method: "POST", body: data, headers };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失敗。");
  return result;
}

function post(path, fields) {
  return request(path, JSON.stringify(fields), { "Content-Type": "application/json" });
}

let accepting = false;
let refreshVersion = 0;
async function refresh() {
  const version = ++refreshVersion;
  const [state, savedTokens] = await Promise.all([
    request(teacher ? "/teacher/status" : "/status"),
    teacher ? request("/teacher/tokens") : null
  ]);
  if (version !== refreshVersion) return;
  accepting = state.accepting;
  if (teacher) {
    byId("tokens").value = savedTokens.codes.join("\n");
    const total = state.seats.filter((row) => row.id).length;
    byId("summary").textContent = `${accepting ? "開放收件中" : "目前未開放收件"} · ${total} / ${state.seats.length} 人已交`;
    byId("accepting").textContent = accepting ? "截止收件" : "開放收件";
    byId("accepting").disabled = false;
    byId("rows").replaceChildren(...state.seats.map((entry) => {
      const row = document.createElement("tr");
      for (const value of [entry.student, entry.resubmit ? "已開放重交" : entry.id ? "已交卷" : entry.paired ? "未交卷" : "尚未登入",
        entry.filename || "—", entry.received ? formatTime(entry.received) : "—"]) {
        row.insertCell().textContent = value;
      }
      const actions = row.insertCell();
      if (entry.id && !entry.resubmit) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.textContent = "退回重交";
        button.setAttribute("aria-label", `退回學號 ${entry.student} 的答案卷`);
        button.addEventListener("click", () => run(button, async () => {
          await post("/teacher/resubmit", { student: entry.student });
          await refresh();
          message.textContent = accepting ? "已允許重交一次，請學生重新整理頁面。"
            : "已允許重交一次，開放收件後學生即可上傳。";
        }));
        actions.append(button);
      } else {
        actions.textContent = entry.resubmit ? "等待學生重交" : "交卷後可退回";
      }
      return row;
    }));
    return;
  }
  byId("identity").textContent = state.paired
    ? `學號末四碼 ${state.last} · ${accepting ? "收件中" : "目前未開放收件"}`
    : "請輸入學號和紙本題目卷上印的 Token。";
  byId("pair-panel").hidden = state.paired;
  byId("upload-panel").hidden = !state.paired || (!!state.submission && !state.resubmit);
  byId("send").textContent = state.resubmit ? "確認重新繳交" : "確認交卷";
  byId("done-panel").hidden = !state.submission;
  byId("send").disabled = !accepting;
  if (state.submission) {
    const { filename, id, received } = state.submission;
    byId("result").textContent = `${filename} · 編號 ${id} · ${formatTime(received)}`;
  }
}

async function run(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  message.textContent = "";
  try {
    await action();
  } catch (error) {
    message.textContent = error.message + " 若畫面未更新，請重新整理確認。";
  } finally {
    button.disabled = button.id === "send" && !accepting;
  }
}

function handleForm(id, action) {
  const form = byId(id);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run(form.querySelector("button"), action);
  });
}

if (teacher) {
  handleForm("seat-form", async () => {
    const result = await post("/teacher/seat", { roster: byId("roster").value });
    await refresh();
    message.textContent = `已確認 ${result.count} 筆學號已登錄。`;
  });
  handleForm("token-form", async () => {
    const result = await post("/teacher/tokens", { count: Number(byId("token-count").value) });
    await refresh();
    message.textContent = `已保存 ${result.codes.length} 組新 Token。`;
  });
  handleForm("reset-form", async () => {
    await post("/teacher/reset", { student: byId("reset-student").value });
    await refresh();
    message.textContent = "已重設登入。請發給該生一組未使用的 Token。";
  });
  byId("accepting").addEventListener("click", (event) => run(event.currentTarget, async () => {
    await post("/teacher/accepting", { accepting: !accepting });
    await refresh();
  }));
} else {
  handleForm("pair", async () => {
    await post("/pair", { student: byId("student").value, code: byId("code").value });
    byId("code").value = "";
    await refresh();
  });
  handleForm("upload", async () => {
    const file = byId("file").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".pdf") || file.size === 0 || file.size > 25 * 1024 * 1024) {
      throw new Error("請選擇一份未超過 25 MiB 的 PDF 答案卷。");
    }
    await request("/submit", file, {
      "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(file.name)
    });
    await refresh();
  });
}
for (const button of document.querySelectorAll(".refresh")) {
  button.addEventListener("click", () => run(button, refresh));
}
refresh().catch(() => { message.textContent = "無法讀取收件狀態，請重新整理或通知監考。"; });
