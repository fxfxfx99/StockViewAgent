import { Alert, App as AntApp, Button, Collapse, Spin, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ReloadOutlined } from "@ant-design/icons";
import * as api from "./api.js";
import { formatDataFiles, formatDataSize, localDataQueryKey, localDataSummary } from "./localDataViewModel.js";

const { Paragraph, Text } = Typography;

export default function LocalDataDirectory({ userId, active }) {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: localDataQueryKey(userId),
    queryFn: ({ signal }) => api.getDataAssets({ signal }),
    enabled: active && Boolean(userId),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: (count, error) => ![403, 404].includes(error?.response?.status) && count < 1,
  });
  const refresh = useMutation({
    mutationFn: api.refreshDataAssets,
    onMutate: () => queryClient.cancelQueries({ queryKey: localDataQueryKey(userId) }),
    onSuccess: (data) => {
      queryClient.setQueryData(localDataQueryKey(userId), data);
      void queryClient.invalidateQueries({ queryKey: ["settings", "data-center", userId] });
      message.success("本地数据索引已刷新");
    },
    onError: () => message.error("索引刷新失败，请稍后重试。"),
  });
  if ([403, 404].includes(query.error?.response?.status)) return null;
  const view = localDataSummary(query.data);
  return <section className="sva-admin-section sva-local-data" aria-label="本地数据目录">
    <div className="sva-admin-section__head"><Text strong>本地数据目录</Text><Button size="small" icon={<ReloadOutlined />} loading={refresh.isPending} disabled={query.isPending || !active} onClick={() => refresh.mutate()}>刷新索引</Button></div>
    {query.isError ? <Alert type="warning" showIcon message="目录信息暂时无法读取" description={query.data ? "下方保留上次读取的统计。" : undefined} action={<Button size="small" loading={query.isFetching} onClick={() => void query.refetch()}>重试</Button>} /> : null}
    {query.isPending ? <Paragraph className="sva-local-data__caption"><Spin size="small" /> 正在读取目录索引…</Paragraph> : <>
      <Text className="sva-local-data__path" copyable={view.path ? { text: view.path } : false}>{view.path || "实际目录暂未提供"}</Text>
      <div className="sva-local-data__totals"><span>总大小 <strong>{formatDataSize(view.size)}</strong></span><span><strong>{formatDataFiles(view.files)}</strong></span></div>
      {view.categories.length ? <dl className="sva-local-data__categories" aria-label="核心资产分类">{view.categories.map((category) => <div key={category.id}><dt>{category.label}</dt><dd>{formatDataFiles(category.files)} · {formatDataSize(category.size_bytes)}{category.retentionLabel ? ` · ${category.retentionLabel}` : ""}</dd></div>)}</dl> : null}
      <Paragraph className="sva-local-data__caption">{view.indexPath ? <>索引：<Text code>{view.indexPath}</Text>。 </> : null}刷新索引会重新扫描已保存的文件。</Paragraph>
      {view.indexGuide ? <Paragraph className="sva-local-data__caption">目录说明保存在 <Text code>{view.indexGuide}</Text>，随数据文件夹一同搬迁。</Paragraph> : null}
      {view.indexTime ? <Paragraph className="sva-local-data__caption">索引更新于 {view.indexTime}</Paragraph> : null}
      {view.commands ? <Collapse size="small" items={[{ key: "migration", label: "搬迁到另一目录或电脑", children: <>
        <Paragraph className="sva-local-data__caption">先停止 API、研究 worker 和其它写入进程，核对并替换命令中的目标路径，再在项目根目录执行。</Paragraph>
        {view.notes.map((note, index) => <Paragraph className="sva-local-data__caption" key={index}>{note}</Paragraph>)}
        <Paragraph className="sva-local-data__migration" copyable={{ text: view.commands, tooltips: ["复制离线迁移步骤", "已复制"] }}>{view.commands}</Paragraph>
      </> }]} /> : <Paragraph className="sva-local-data__caption">搬迁前请停止所有写入进程，保留整个数据目录；迁移命令暂未提供。</Paragraph>}
    </>}
  </section>;
}
