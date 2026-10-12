import React from "react";
import { HashRouter, NavLink, Route, Routes } from "react-router-dom";
import { AppProvider, ROLES, useApp } from "./ui";
import { Overview, DeviceDetail } from "./pages/devices";
import { Tasks, Takeovers } from "./pages/tasks";
import { Audit, CapabilitiesPlugins, Settings } from "./pages/admin";

const Layout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { role, connected, lastEventAt } = useApp();
  return (
    <div className="layout">
      <header className="topbar">
        <div className="brand">Arm Fleet 设备控制台</div>
        <nav>
          <NavLink to="/" end>设备总览</NavLink>
          <NavLink to="/tasks">任务中心</NavLink>
          <NavLink to="/takeovers">异常与恢复</NavLink>
          <NavLink to="/capabilities">能力与插件</NavLink>
          <NavLink to="/audit">审计日志</NavLink>
          <NavLink to="/settings">运行设置</NavLink>
        </nav>
        <div className="topbar-right">
          <span className={connected ? "conn conn-ok" : "conn conn-bad"}>
            {connected ? "实时" : `连接已断开${lastEventAt ? `（最后更新 ${lastEventAt}）` : ""}`}
          </span>
          <span className="muted">当前角色：{ROLES[role] ?? role}</span>
        </div>
      </header>
      <main>{children}</main>
      <footer className="footer muted">
        Arm Fleet 是机械臂、摄像头、设备锁、租约、归位、急停和物理状态的唯一可写事实源；控制台为运维界面，不含任何业务数据。
      </footer>
    </div>
  );
};

const App: React.FC = () => (
  <AppProvider>
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/devices/:deviceId" element={<DeviceDetail />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/takeovers" element={<Takeovers />} />
          <Route path="/capabilities" element={<CapabilitiesPlugins />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </Layout>
    </HashRouter>
  </AppProvider>
);

export default App;
