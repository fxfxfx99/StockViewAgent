import { useLayoutEffect } from "react";
import { Button, Dropdown, Tooltip } from "antd";
import { CalendarOutlined, DatabaseOutlined, ExperimentOutlined, LineChartOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";
import RadarChartOutlined from "@ant-design/icons/es/icons/RadarChartOutlined";
import GlobalOutlined from "@ant-design/icons/es/icons/GlobalOutlined";
import FundOutlined from "@ant-design/icons/es/icons/FundOutlined";
import EyeOutlined from "@ant-design/icons/es/icons/EyeOutlined";
import PlaySquareOutlined from "@ant-design/icons/es/icons/PlaySquareOutlined";
import DashboardOutlined from "@ant-design/icons/es/icons/DashboardOutlined";
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
        <Link to="/" className={active === "market" ? "is-active" : undefined} aria-current={active === "market" ? "page" : undefined}><LineChartOutlined aria-hidden="true" /><span>关注个股</span></Link>
        <Link to="/research" className={active === "research" ? "is-active" : undefined} aria-current={active === "research" ? "page" : undefined}><ExperimentOutlined aria-hidden="true" /><span>研究</span></Link>
        <Link to="/monitor" className={active === "monitor" ? "is-active" : undefined} aria-current={active === "monitor" ? "page" : undefined}><EyeOutlined aria-hidden="true" /><span>监控</span></Link>
        <Link to="/replay" className={active === "replay" ? "is-active" : undefined} aria-current={active === "replay" ? "page" : undefined}><PlaySquareOutlined aria-hidden="true" /><span>K线复盘</span></Link>
        <Link to="/merrill-clock" className={active === "merrill-clock" ? "is-active" : undefined} aria-current={active === "merrill-clock" ? "page" : undefined}><DashboardOutlined aria-hidden="true" /><span>美林时钟</span></Link>
      </nav>
      <div className="sva-app-toolbar__actions">
        <Dropdown trigger={["click"]} menu={{ items: [
          { key: "today", icon: <RadarChartOutlined />, label: <Link to="/today">今日变化</Link> },
          { key: "lab", icon: <ExperimentOutlined />, label: <Link to="/lab">量化实验室</Link> },
          { key: "macro", icon: <FundOutlined />, label: <Link to="/macro">市场研判</Link> },
          { key: "calendar", icon: <CalendarOutlined />, label: <Link to="/calendar">经济日历</Link> },
          { key: "news", icon: <GlobalOutlined />, label: <Link to="/news">海外快讯</Link> },
          { key: "data", icon: <DatabaseOutlined />, label: <Link to="/data">数据中心</Link> },
        ] }}><Button type="text" aria-label="打开辅助工具">工具</Button></Dropdown>
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
