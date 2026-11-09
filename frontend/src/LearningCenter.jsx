import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button, Checkbox, Layout, Progress, Tag } from "antd";
import ArrowRightOutlined from "@ant-design/icons/es/icons/ArrowRightOutlined";
import ExportOutlined from "@ant-design/icons/es/icons/ExportOutlined";
import CheckCircleFilled from "@ant-design/icons/es/icons/CheckCircleFilled";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { LEARNING_RESOURCES, RESOURCE_REVIEWED_AT } from "./learningResources.js";
import { LEARNING_STEP_IDS, learningLevel, learningStorageKey, readLearningPreferences } from "./learningPreferences.js";
import "./LearningCenter.css";

const PATHS = {
  beginner: {
    title: "从认识钱，到看懂一家公司",
    description: "按顺序学，每次完成一个小任务。无需编程，也无需先买入股票。",
    steps: [
      { id: "money", title: "先理清自己的收支", duration: "约 20 分钟", text: "分清收入、必要支出、负债和近期要用的钱；认识复利与通胀。", task: "用纸笔列出一个月的收支，标出近期必须用到的资金。愿意动手的话，用预算演示练习。", resource: "actual-budget" },
      { id: "products", title: "认识股票、基金和风险", duration: "分 2–3 次学习", text: "了解买股票与买基金分别持有什么，再认识指数、分散投资和亏损。", task: "看完「入市第一课」，用自己的话解释股票与基金的区别，以及为什么分散后仍可能亏损。", resource: "sse-first-steps" },
      { id: "reading", title: "选一本书，带着问题读", duration: "每天 20 分钟", text: "用 Financial Freedom 找到入门书单，从一个主题开始，逐步建立判断方法。", task: "挑一本入门书，记录三个新概念、一条不理解的观点，以及需要查证的事实。", resource: "financial-freedom" },
      { id: "observation", title: "完成第一次观察记录", duration: "约 30 分钟", text: "选一家熟悉的公司，了解它卖什么、靠什么赚钱，再对照公司资料和财报原文。", task: "记下业务、一个待核实问题和原文出处；把新闻中的事实与自己的猜测分开。", to: "/", action: "去关注个股" },
    ],
  },
  experienced: {
    title: "把知识变成可复查的研究",
    description: "已经理解股票、基金和基本风险，就从财报、证据与复盘开始。量化编程是可选方向。",
    steps: [
      { id: "reports", title: "读一份完整财报", duration: "财报阅读", text: "把利润表、资产负债表、现金流量表联系起来，核对报告期、单位和审计意见。", task: "选一家公司，比较营收、净利润与经营现金流，写出差异及原文页码。", resource: "sse-annual-reports" },
      { id: "evidence", title: "建立一份研究档案", duration: "证据与判断", text: "围绕一个具体问题整理材料，区分事实、假设与反向证据。", task: "写下研究问题、当前判断、支持证据和会让你改变判断的条件。", to: "/research", action: "去建立研究" },
      { id: "review", title: "用历史复盘检验判断", duration: "观察与复盘", text: "先记录当时能知道的信息，再逐日回放；比较判断如何随证据变化。", task: "保留一次判断失误，说明当时遗漏了什么。需要持续观察时再建立监控计划。", to: "/replay", action: "去 K 线复盘" },
    ],
  },
};

const CONCEPTS = [
  ["股票、基金、ETF 有什么区别？", "股票代表公司的一部分所有权；基金把投资者的资金集合起来，按约定的策略投资。ETF 是可在交易所买卖的基金，许多 ETF 跟踪指数。基金和 ETF 都可能亏损。"],
  ["复利是不是保证赚钱？", "复利是把收益留在本金中继续参与计算。例如每年都增长 5%，100 元两年变成 110.25 元；这是数学假设，投资回报会波动，也可能为负。"],
  ["回撤与涨跌幅有什么不同？", "回撤衡量从此前高点跌了多少。100 元跌到 80 元是 20% 回撤，但从 80 元回到 100 元需要上涨 25%。短期涨跌幅只比较选定区间的起点和终点。"],
  ["公司赚钱，为什么现金可能减少？", "收入、利润和实际收付现金的时间可能不同。例如确认销售收入后，客户还未付款。读财报时需要结合应收账款、经营现金流和利润，不能只看一个数字。"],
  ["历史回测好，未来就会好吗？", "回测是在历史数据上模拟规则。把未来信息带进过去、反复挑选最好的参数、遗漏手续费和无法成交的情况，都可能夸大结果。历史表现不能保证未来收益。"],
];

function ResourceCard({ resource }) {
  return <article className="learning-resource" id={`resource-${resource.id}`}>
    <div className="learning-resource__tags"><Tag>{resource.kind}</Tag><span>{resource.language}</span></div>
    <h3>{resource.title}</h3>
    <p>{resource.description}</p>
    <dl>
      <div><dt>适合谁</dt><dd>{resource.prerequisite}</dd></div>
      <div><dt>从哪开始</dt><dd>{resource.startWith}</dd></div>
      <div><dt>读后练习</dt><dd>{resource.exercise}</dd></div>
    </dl>
    <p className="learning-resource__caution">{resource.caution}</p>
    <div className="learning-resource__actions">
      <a href={resource.url} target="_blank" rel="noopener noreferrer">打开资料 <ExportOutlined /></a>
      {resource.practiceUrl ? <a href={resource.practiceUrl} target="_blank" rel="noopener noreferrer">打开演示 <ExportOutlined /></a> : null}
    </div>
    <details className="learning-source"><summary>来源与授权</summary><p>{resource.license}</p>
      {resource.repoUrl ? <a href={resource.repoUrl} target="_blank" rel="noopener noreferrer">GitHub 仓库 ↗</a> : null}
      {resource.licenseUrl ? <a href={resource.licenseUrl} target="_blank" rel="noopener noreferrer">查看授权说明 ↗</a> : null}
    </details>
  </article>;
}

export default function LearningCenter() {
  const { user } = useAuth();
  return <LearningWorkspace key={user?.id ?? "guest"} />;
}

function LearningWorkspace() {
  const { user, authRequired, logout } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const storageKey = learningStorageKey(user?.id);
  const [preferences, setPreferences] = useState(() => {
    try { return readLearningPreferences(window.localStorage, storageKey); }
    catch { return { level: "beginner", completed: [] }; }
  });
  const [saveFailed, setSaveFailed] = useState(false);
  const pathTitleRef = useRef(null);
  const focusPathAfterChange = useRef(false);
  const requestedLevel = learningLevel(searchParams.get("level"));
  const level = requestedLevel || preferences.level;
  const path = PATHS[level];
  const completed = LEARNING_STEP_IDS[level].filter((id) => preferences.completed.includes(id)).length;
  const resources = LEARNING_RESOURCES.filter((resource) => resource.level === level);

  // Give the entry state an explicit URL so Back restores it after a level change.
  useEffect(() => {
    if (!requestedLevel) {
      setSearchParams((previous) => {
        const next = new URLSearchParams(previous);
        next.set("level", preferences.level);
        return next;
      }, { replace: true });
    }
  }, [requestedLevel, preferences.level, setSearchParams]);

  useEffect(() => {
    if (focusPathAfterChange.current) {
      focusPathAfterChange.current = false;
      pathTitleRef.current?.focus({ preventScroll: true });
      pathTitleRef.current?.scrollIntoView({ block: "start" });
    }
  }, [level]);

  function save(next) {
    setPreferences(next);
    try { window.localStorage.setItem(storageKey, JSON.stringify(next)); setSaveFailed(false); }
    catch { setSaveFailed(true); }
  }

  function chooseLevel(nextLevel) {
    save({ ...preferences, level: nextLevel });
    setSearchParams((previous) => { const next = new URLSearchParams(previous); next.set("level", nextLevel); return next; });
  }

  function toggleStep(id, checked) {
    save({ level, completed: checked ? [...new Set([...preferences.completed, id])] : preferences.completed.filter((item) => item !== id) });
  }

  return <Layout className="sva-layout learning-page">
    <AppToolbar active="learn" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <main className="learning-main">
      <header className="learning-heading"><h1>学习入门</h1><p>选择适合当前基础的路径，按步骤完成练习。</p></header>
      <div className="learning-levels" role="group" aria-label="选择学习基础">
        <button type="button" aria-pressed={level === "beginner"} className={level === "beginner" ? "is-selected" : ""} onClick={() => chooseLevel("beginner")}>
          <span className="learning-levels__title">我是零基础 {level === "beginner" ? <CheckCircleFilled /> : null}</span>
          <span>还不熟悉股票、基金、收益与风险，从概念和阅读开始。</span>
        </button>
        <button type="button" aria-pressed={level === "experienced"} className={level === "experienced" ? "is-selected" : ""} onClick={() => chooseLevel("experienced")}>
          <span className="learning-levels__title">我有基础 {level === "experienced" ? <CheckCircleFilled /> : null}</span>
          <span>已懂常见投资品种与亏损风险，想练习财报、研究和复盘。</span>
        </button>
      </div>
      <p className="learning-choice-note">不确定就从零基础开始，随时可以切换。有交易经历也可以回来补基础。</p>

      <section className="learning-path" aria-labelledby="learning-path-title">
        <div className="learning-section-heading"><div><h2 id="learning-path-title" ref={pathTitleRef} tabIndex={-1}>{path.title}</h2><p>{path.description}</p></div>
          <div className="learning-progress" aria-live="polite"><span>已完成 {completed} / {path.steps.length}</span><Progress percent={Math.round(completed / path.steps.length * 100)} showInfo={false} size="small" /></div>
        </div>
        <ol className="learning-steps">{path.steps.map((step, index) => <li key={step.id} className={preferences.completed.includes(step.id) ? "is-completed" : ""}>
          <span className="learning-step-number">{String(index + 1).padStart(2, "0")}</span>
          <div className="learning-step-content"><div className="learning-step-heading"><h3>{step.title}</h3><span>{step.duration}</span></div><p>{step.text}</p><p className="learning-task"><strong>试着做：</strong>{step.task}</p>
            <div className="learning-step-actions">{step.to ? <Link to={step.to}>{step.action} <ArrowRightOutlined /></Link> : <a href={`#resource-${step.resource}`}>查看推荐资料 <ArrowRightOutlined /></a>}<Checkbox aria-label={`已完成：${step.title}`} checked={preferences.completed.includes(step.id)} onChange={(event) => toggleStep(step.id, event.target.checked)}>完成练习</Checkbox></div>
          </div>
        </li>)}</ol>
        <p className="learning-storage-note" role="status">{saveFailed ? "当前浏览器无法保存，选择与进度仅在本次页面停留期间有效。" : "选择与进度按账户保存在当前浏览器；勾选表示完成练习，可随时取消。"}</p>
      </section>

      <section className="learning-library" aria-labelledby="learning-library-title">
        <div className="learning-section-heading"><div><h2 id="learning-library-title">{level === "beginner" ? "零基础精选资料" : "有基础 · 延伸学习"}</h2><p>{level === "beginner" ? "优先选择可直接阅读的资料；预算演示为英文可选练习。" : "先读财报；愿意学习 Python，再进入量化教程与研究框架。"}</p></div><span className="learning-review-date">资料核对于 {RESOURCE_REVIEWED_AT}</span></div>
        <div className="learning-resources">{resources.map((resource) => <ResourceCard key={resource.id} resource={resource} />)}</div>
      </section>

      {level === "beginner" ? <section className="learning-concepts" aria-labelledby="learning-concepts-title"><h2 id="learning-concepts-title">先弄懂这几个概念</h2>{CONCEPTS.map(([title, text]) => <details key={title}><summary>{title}</summary><p>{text}</p></details>)}
        <div className="learning-next"><div><h3>什么时候可以进入下一步？</h3><p>能解释常见品种与亏损风险，并写下一条有出处的观察，就可以试试有基础路径。</p></div><Button onClick={() => { focusPathAfterChange.current = true; chooseLevel("experienced"); }}>看看有基础路径 <ArrowRightOutlined /></Button></div>
      </section> : <div className="learning-next"><div><h3>把待验证的问题留下来</h3><p>研究中发现的新问题，可以带入档案；需要跟踪的条件，可以建立观察计划。</p></div><Link to="/monitor">去监控 <ArrowRightOutlined /></Link></div>}
      <footer className="learning-footer">资料用于学习与研究，不代表收益承诺。阅读作者观点时，结合原始资料核对；交易规则以交易所最新公告为准。</footer>
    </main>
  </Layout>;
}
