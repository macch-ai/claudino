import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("claudino", {
  auth: () => ipcRenderer.invoke("claude:auth"),
  ask: (prompt: string) => ipcRenderer.invoke("claude:ask", prompt),
  logout: () => ipcRenderer.invoke("claude:logout"),
  hubState: () => ipcRenderer.invoke("hub:state"),
  onHubState: (cb: (s: unknown) => void) => ipcRenderer.on("hub:state", (_e, s) => cb(s)),
  pair: () => ipcRenderer.invoke("hub:pair"),
  login: () => ipcRenderer.invoke("hub:login"),
  unpair: () => ipcRenderer.invoke("hub:unpair"),
  setHubUrl: (url: string) => ipcRenderer.invoke("hub:setUrl", url),
  info: () => ipcRenderer.invoke("app:info"),
  setLaunchAtLogin: (on: boolean) => ipcRenderer.invoke("app:launchAtLogin", on),
  open: (url: string) => ipcRenderer.invoke("app:open", url),
  automations: () => ipcRenderer.invoke("automations:list"),
  saveAutomation: (a: unknown) => ipcRenderer.invoke("automations:save", a),
  deleteAutomation: (id: string) => ipcRenderer.invoke("automations:delete", id),
  runAutomation: (id: string) => ipcRenderer.invoke("automations:run", id),
  setTheme: (mode: string) => ipcRenderer.invoke("app:theme", mode),
  onAutomations: (cb: (list: unknown) => void) => ipcRenderer.on("automations:state", (_e, l) => cb(l)),
});
