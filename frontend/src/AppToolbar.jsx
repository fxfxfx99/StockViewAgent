import { useLayoutEffect } from "react";
import { Button, Dropdown, Tooltip } from "antd";
import { CalendarOutlined, DatabaseOutlined, ExperimentOutlined, LineChartOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";
import RadarChartOutlined from "@ant-design/icons/es/icons/RadarChartOutlined";
import { Link, useLocation } from "react-router-dom";
import ThemeModeToggle from "./ThemeModeToggle.jsx";

/** Shared application chrome. Page actions are slots to keep navigation independent. */
export default function AppToolbar({ active = "market", settingsAction, user, authRequired, onLogout, leadingAction }) {
  const { pathname } = useLocation();
  useLayoutEffect(() => { window.scrollTo({ top: 0, left: 0, behavior: "instant" }); }, [pathname]);
  return (
    <header className="sva-app-toolbar">
      <div className="sva-app-toolbar__leading">
        {leadingAction}
        <Link to="/" className="sva-app-brand" aria-label="StockViewAgent 首页">
          <svg className="sva-app-brand__mark" viewBox="0 0 30 30" fill="none" aria-hidden="true">
            <path d="m3 20 4-9 4 13 5-20 4 19 4-12 3 5" stroke="currentColor" strokeWidth="2.7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>StockViewAgent</span>
        </Link>
      </div>
      <nav className="sva-app-nav" aria-label="主要导航">
        <Link to="/today" className={active === "today" ? "is-active" : undefined} aria-current={active === "today" ? "page" : undefined}>
          <RadarChartOutlined aria-hidden="true" /><span>今日变化</span>
        </Link>
        <Link to="/" className={active === "market" ? "is-active" : undefined} aria-current={active === "market" ? "page" : undefined}>
          <LineChartOutlined aria-hidden="true" /><span>行情工作台</span>
        </Link>
        <Link to="/research" className={active === "research" ? "is-active" : undefined} aria-current={active === "research" ? "page" : undefined}>
          <ExperimentOutlined aria-hidden="true" /><span>研究实验室</span>
        </Link>
        <Link to="/calendar" className={active === "calendar" ? "is-active" : undefined} aria-current={active === "calendar" ? "page" : undefined}>
          <CalendarOutlined aria-hidden="true" /><span>经济日历</span>
        </Link>
        <Link to="/data" className={active === "data" ? "is-active" : undefined} aria-current={active === "data" ? "page" : undefined}>
          <DatabaseOutlined aria-hidden="true" /><span>数据中心</span>
        </Link>
      </nav>
      <div className="sva-app-toolbar__actions">
        <ThemeModeToggle />
        {settingsAction ? <><span className="sva-toolbar-divider" aria-hidden="true" />{settingsAction}</> : null}
        {authRequired && user ? (
          <Dropdown menu={{ items: [{ key: "logout", icon: <LogoutOutlined />, label: "退出登录", onClick: onLogout }] }} trigger={["click"]}>
            <Tooltip title={user.username || "当前账户"}>
              <Button type="text" icon={<UserOutlined />} aria-label={`账户：${user.username || "当前用户"}`} />
            </Tooltip>
          </Dropdown>
        ) : null}
      </div>
    </header>
  );
}
