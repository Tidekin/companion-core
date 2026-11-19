const byId = (id) => document.getElementById(id);
const workerLabels = new Map(Object.entries({
  RECORDED: "近期有运行记录", WAITING: "最近进入等待", STALE: "记录已过期",
  UNOBSERVED: "尚无记录", INVALID: "记录异常",
}));
const runtimeLabels = new Map([["READY", "运行正常"], ["ATTENTION", "有状态需要查看"]]);

function notice(message) {
  byId("metrics").hidden = true;
  byId("updated").textContent = "";
  byId("status").dataset.ready = "false";
  byId("status").textContent = message;
}

function show(data) {
  const {runtime, counts, observed_at: stamp} = data;
  const worker = workerLabels.get(runtime.worker.state);
  const status = runtimeLabels.get(runtime.state);
  const {available, paused} = runtime.modes.proactive;
  const observed = new Date(stamp);
  if (!worker || !status || typeof available !== "boolean" || typeof paused !== "boolean" ||
      ![counts.pending_jobs, counts.pending_deliveries].every(n => Number.isSafeInteger(n) && n >= 0) ||
      typeof stamp !== "string" || !Number.isFinite(observed.getTime())) {
    throw new Error("Invalid overview");
  }
  byId("worker").textContent = worker;
  byId("proactive").textContent = !available ? "未启用" : paused ? "已暂停" : "已开启";
  byId("jobs").textContent = counts.pending_jobs;
  byId("deliveries").textContent = counts.pending_deliveries;
  byId("updated").textContent = "更新于 " + observed.toLocaleString("zh-CN", {hour12: false});
  byId("status").textContent = status;
  byId("status").dataset.ready = String(runtime.state === "READY");
  byId("metrics").hidden = false;
}

let reading = false;
async function refresh() {
  if (document.hidden || reading) return;
  reading = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("/v1/console/overview", {
      credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal,
    });
    if (response.status === 401 || response.status === 403) {
      notice("请进入此实例控制台登录，再返回总览。");
    } else if (response.ok) {
      show(await response.json());
    } else {
      notice("暂时无法读取状态，请进入控制台查看。");
    }
  } catch {
    notice("暂时无法读取状态，请进入控制台查看。");
  } finally {
    clearTimeout(timer);
    reading = false;
  }
}

document.addEventListener("visibilitychange", refresh);
window.addEventListener("pageshow", refresh);
setInterval(refresh, 60000);
