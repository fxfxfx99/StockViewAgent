import { Button, Tooltip } from "antd";
import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { useThemeMode } from "./ThemeModeProvider.jsx";

export default function ThemeModeToggle() {
  const { isLight, toggle } = useThemeMode();
  return (
    <Tooltip title={isLight ? "切换深色模式" : "切换浅色模式"}>
      <Button
        type="text"
        className="sva-header-settings"
        icon={isLight ? <MoonOutlined /> : <SunOutlined />}
        onClick={toggle}
        aria-label={isLight ? "深色模式" : "浅色模式"}
      />
    </Tooltip>
  );
}
