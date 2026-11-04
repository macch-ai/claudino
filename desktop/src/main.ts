// claudino desktop: menu bar app that runs this teammate's Slack requests with their own Claude login.
import { app, BrowserWindow, dialog, ipcMain, nativeImage, shell, Tray } from "electron";
import fs from "node:fs";
import path from "node:path";
import { ask, authStatus, logout } from "./claude";
import * as automations from "./automations";
import { Config, DEFAULT_URL, HubClient, loadConfig, saveConfig } from "./hub";

let tray: Tray | null = null;
let win: BrowserWindow | null = null;

const TOOLTIPS = {
  "no-config": "MACCH: sin configurar",
  connecting: "MACCH: conectando con hermes…",
  connected: "MACCH: conectada a hermes",
  denied: "MACCH: token rechazado",
  offline: "MACCH: sin conexión con hermes",
} as const;

function hubState() {
  const { token, ...profile } = loadConfig(); // the token never reaches the renderer
  return { status: hub.status, running: hub.running, tasks: hub.tasks, pairing: hub.pairing,
           profile: { url: DEFAULT_URL, ...profile } };
}

function send(channel: string, data: unknown): void {
  if (win && !win.isDestroyed()) win.webContents.send(channel, data);
}

function automationsState() {
  return automations.load().map((a) => ({ ...a, nextRunAt: a.enabled ? automations.nextRun(a) : null }));
}

const scheduler = new automations.Scheduler(
  (a, trigger, work) => {
    hub.track(a.name, work);
    work.finally(() => send("automations:state", automationsState())).catch(() => undefined);
    send("automations:state", automationsState());
  },
  (a, r) => {
    if (!hub.post(a.channel!, a.name, r.result || "(sin respuesta)")) {
      console.warn(`automation ${a.name}: not connected to hermes, result not posted to Slack`);
    }
  },
);

// Slack "@claudino ejecutá <name>": hermes asks this Mac to run one of its automations.
function runFromSlack(name: string) {
  const a = automations.findByName(name);
  if (!a) return Promise.reject(new Error(`no hay ninguna automatización llamada "${name}" en esta Mac`));
  if (!a.enabled) return Promise.reject(new Error(`la automatización "${a.name}" está desactivada`));
  return scheduler.run(a, "slack");
}

const hub = new HubClient(() => {
  tray?.setTitle(hub.running ? String(hub.running) : "");
  tray?.setToolTip(TOOLTIPS[hub.status] + (hub.running ? ` · ${hub.running} en curso` : ""));
  send("hub:state", hubState());
}, runFromSlack);

function showWindow(): void {
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
    return;
  }
  win = new BrowserWindow({
    width: 780,
    height: 560,
    minWidth: 640,
    minHeight: 440,
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 18 },
    backgroundColor: "#161616",
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  win.loadFile(path.join(__dirname, "../renderer/index.html"));
  // Dev aid: CLAUDINO_SCREENSHOT=/path.png saves a capture of the window once it has rendered
  // (CLAUDINO_SCREENSHOT_PAGE=connection|tasks|automations|about picks the page).
  const shot = process.env.CLAUDINO_SCREENSHOT;
  const page = process.env.CLAUDINO_SCREENSHOT_PAGE;
  if (shot) {
    win.webContents.once("did-finish-load", async () => {
      if (page) await win!.webContents.executeJavaScript(`document.querySelector('[data-page="${page.replace(/\W/g, "")}"]').click()`);
      const delay = Number(process.env.CLAUDINO_SCREENSHOT_DELAY_MS) || 2500;
      setTimeout(async () => fs.writeFileSync(shot, (await win!.webContents.capturePage()).toPNG()), delay);
    });
  }
}

app.whenReady().then(() => {
  app.dock?.hide();
  // MACCH logo as a template image: macOS tints it for light and dark menu bars (trayTemplate@2x.png for Retina).
  const icon = nativeImage.createFromPath(path.join(__dirname, "../assets/trayTemplate.png"));
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.on("click", showWindow);
  hub.start();
  if (hub.status === "no-config") hub.pair();
  scheduler.start();
  showWindow();
});

ipcMain.handle("automations:list", () => automationsState());
ipcMain.handle("automations:save", (_e, a) => {
  automations.upsert(a);
  return automationsState();
});
ipcMain.handle("automations:delete", (_e, id: string) => {
  automations.remove(id);
  return automationsState();
});

ipcMain.handle("claude:auth", () => authStatus());
ipcMain.handle("claude:ask", (_e, prompt: string) => ask(prompt));
ipcMain.handle("claude:logout", async () => {
  const { response } = await dialog.showMessageBox({
    type: "warning",
    buttons: ["Cerrar sesión", "Cancelar"],
    defaultId: 1,
    cancelId: 1,
    message: "¿Cerrar la sesión de Claude Code en esta Mac?",
    detail: "Afecta a todo Claude Code en esta Mac, no solo a claudino. Para volver: claude auth login.",
  });
  if (response !== 0) return false;
  await logout();
  return true;
});
ipcMain.handle("hub:state", () => hubState());
ipcMain.handle("hub:pair", () => hub.pair());
ipcMain.handle("hub:unpair", async () => {
  const { response } = await dialog.showMessageBox({
    type: "warning",
    buttons: ["Desvincular", "Cancelar"],
    defaultId: 1,
    cancelId: 1,
    message: "¿Desvincular esta Mac de Slack?",
    detail: "Tus pedidos a @claudino dejan de correr acá hasta que la vuelvas a vincular.",
  });
  if (response === 0) hub.unpair();
});
ipcMain.handle("hub:setUrl", (_e, url: string) => {
  if (!/^wss?:\/\/\S+$/.test(url)) return;
  saveConfig({ ...(loadConfig() as Config), url });
  hub.start();
});
ipcMain.handle("app:info", () => ({
  version: app.getVersion(),
  launchAtLogin: app.getLoginItemSettings().openAtLogin,
}));
ipcMain.handle("app:launchAtLogin", (_e, on: boolean) => app.setLoginItemSettings({ openAtLogin: on }));
ipcMain.handle("app:open", (_e, url: string) => {
  if (/^https:\/\/(claude\.ai|github\.com)\//.test(url)) return shell.openExternal(url);
});
// Browser login: only the URL hermes sent with the current pairing code.
ipcMain.handle("hub:login", () => {
  const url = hub.pairing?.loginUrl;
  if (url && /^https:\/\//.test(url)) return shell.openExternal(url);
});

// Menu bar app: keep running when the window closes.
app.on("window-all-closed", () => undefined);
