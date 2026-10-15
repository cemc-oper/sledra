# Sledra 契约 1.0.0 草案（SLD-02）

这是公开字节格式的开发基线，不是已发行契约或记录器。[1.0.0/](1.0.0/)保存可搬运资源，[build_contract.py](build_contract.py)重新生成。schema采用[JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12/)，无远端$ref；方言URI不在校验时抓取。canonical资源显式重新生成后运行python3 scripts/sync_contracts.py，check在私有副本核对embed字节。24类event-types目录包含已知payload、关联层次、source kind和producer role，权威来源见[契约来源](../docs/source/reference/contracts.md)。capabilities分别声明read_contracts、write_contracts、known_event_types，不与软件版本混合。

## 文档与语义校验

| 文档 | 结构schema | JSON Schema之外的校验 |
|---|---|---|
| DefinitionManifest | definition_manifest.schema.json | definition_id=def_+内容摘要，排除自身ID/摘要；task key唯一、stage/dependency引用、默认参数type/enum |
| RunContext | run_context.schema.json | definition/expectation引用、展开参数和唯一scope、context摘要、选定expectation的cycle |
| AttemptContext | attempt_context.schema.json | 父Run摘要、scope内task/参数、task instance重新推导、retry非自身、context摘要 |
| JobRef | job_ref.schema.json | platform/scheduler/cluster/job ID/submission generation构成身份，父Run/Attempt/platform一致，复用job号仍不同身份 |
| ArtifactVersion | artifact.schema.json | cycle/producer父身份，版本由实际内容推导而非仅路径，Attempt必有Run |
| ExpectedCycle/ProductExpectation | expectation.schema.json | 内容推导plan ID，cycle/slot唯一，取消原因，Run的system/purpose/deployment/cycle一致 |
| Event | event.schema.json及event-types.json | role/source权威、关联、exit条件、时间基准、必要证据、finish集合摘要、注册context绑定；同ID异内容冲突 |

辅助工具验证含各文档及事件列表的bundle，使测试有足够父上下文核对身份和权威。这不是producer/ingest服务。未来独立校验可按不可变引用加载SLEDRA_ROOT父文档；父级缺失必须失败，不能查“最新Run”。`engine.state_observed.payload.node_path` 是逻辑节点标识，可为 `/suite/family/task`，不作为文件 locator。

除明确开放字段外，对象additionalProperties=false，已知enum闭合。未知数据置于extensions的vendor.feature命名空间键，每值为对象。有效1.x信封的未知event_type保留为不透明记录，关联和扩展不丢失，不推导成功。task.success保留且拒绝，因为单一producer无法同时证明进程、引擎、产品和交付成功。

时间须RFC3339并含Z或数值offset，偏移小时为00～23、分钟为00～59；`-00:00` 保留本地偏移未知含义。occurred_at=null要求time_quality.reason，recorded_at不能代替发生时间；直接process/stage边界必须有已知发生时间。duration_ns/lead_seconds为单位明确的有符号64位安全整数，适用位置非负。seq只在session内有效；普通event_id为UUIDv4，稳定source/注册为evt_加完整SHA256；重传保持原ID和完整信封。未知事件若带attempt_id，也必须带task_instance_id。

证据引用含ID、受控locator、source及present/expired/unknown，可选摘要、字节范围、generation和保留时间。locator不证明文件仍在，byte_end不得早于byte_start。提交绑定JobRef与submission generation，artifact绑定精确版本；交付绑定expectation slot、artifact version/ID、recipient、rule version和父文档。复用scheduler job号必须使用不同submission_instance。

## 规范身份字节

1. 参数名为ASCII snake_case，先在DefinitionManifest声明。默认值在保存scope/context前展开，未声明/缺必需/错类型/null拒绝；所有声明值参与身份。
2. 字符串与键规范化为Unicode NFC，对象键按字典序排序，省略无意义空白，输出不作ASCII转义的UTF-8，转义控制字符和U+2028/U+2029。成对surrogate解码为单一scalar，孤立surrogate拒绝。值仅使用schema允许的精确有符号64位整数、字符串、bool或null，禁止float/NaN，JSON -0规范为0。字符串"01"与整数1不同。上游0h/0s先转为lead_seconds整数0，原单位字符串不是合法值。
3. task_instance_id为ti_+SHA256(length_pack(run_id,task_key,canonical_parameters_UTF8))。length_pack对每份NFC UTF-8先写8字节无符号大端字节长度，再写原字节，不添加分隔符/NUL。
4. Run为run_+SHA256(length_pack(registry_id,"run",deployment_id,launch_token))；Attempt为att_+SHA256(length_pack(registry_id,"attempt",task_instance_id,execution_token))；JobRef为job_+SHA256(length_pack(platform_id,scheduler,cluster_id,scheduler_job_id,submission_instance))。原token归调用方，公开摘要不可恢复。同token异request是冲突，不发新ID；request摘要覆盖规范化请求对象。
5. content_sha256/context_sha256只排除自身摘要字段，DefinitionManifest还排除definition_id。artifact/expectation ID只排除自身ID；事件canonical hash覆盖完整事件，采集raw bytes/offset是独立事实。

[golden-vectors.json](fixtures/1.0.0/golden-vectors.json)给出精确字节和完整ID/hash。[SHA256SUMS](fixtures/1.0.0/SHA256SUMS)固定全部合成夹具，不使用历史GPL测试文本或真实运行日志。

## 兼容矩阵

| 写端 → 读端 | 处理 |
|---|---|
| 1.0 → 1.0 | 校验已知语义，同ID同完整信封为重传 |
| 后续1.x → 1.0 | 稳定信封/扩展可读，未知类型保留不透明并报语义不支持，不算成功 |
| 2.x → 1.x | unsupported_major；未来采集保存原bytes隔离，不套1.x语义 |
| legacy ecFlow log → 1.x | 来源/许可/时区证据齐备后由16适配，保留observed/inferred来源 |

新增可选字段只有位于既有开放extension时才是minor；新增核心enum、必需字段、单位、身份算法或语义须新major或event type。读端与回放向量升级前记录端固定write版本；future-extension正例核对未知对象/扩展完整保留。

## CLI与错误

[帮助/版本及默认资源](../docs/source/reference/cli.md)无需外部契约；显式覆盖必须准确兼容。context/lifecycle与sledra event已实现。bundle工具为python3 spec/check.py BUNDLE.json，或recorder/下go run ./cmd/sledra-spec-check [-spec-dir DIR] BUNDLE.json，成功stdout为JSON状态，失败stderr为{ok:false,code,detail}。离线[sledra-event](../recorder/event/README.md)编码/校验不写spool，stdout为空。

| 记录命令 | 输入 | 行为 |
|---|---|---|
| registry run-create --request-file FILE --context-out FILE | 显式launch token、registry/deployment/system/cycle、definition_file、可选expectation和scope，SLEDRA_ROOT | 空stdout、原子不可变context，相同请求复用赢家 |
| registry attempt-create --context RUN --request-file FILE --context-out FILE | 父Run、task key/完整参数、execution token、可选retry_of | 空stdout、不可变Attempt，新token创建新尝试 |
| context validate --file FILE | context及不可变父引用 | 缺失/非法非零，不生成可执行Shell |
| event --context ATTEMPT --type TYPE --payload-file FILE [--event-id ID] [--strict] | context、类型化payload、部署role | 空stdout，business观测失败有界诊断并返回0，strict返回错误；同ID重传须完整信封 |
| event --type attempt.started --invocation-id ID --process-role ROLE，或attempt.exited --process-role ROLE (--exit-code N或--signal N) | 配合--context，用Go构造进程事实 | 同事件策略，[Shell](../integrations/bash/README.md)先保存rc/signal，不拼JSON |
| run finish --context RUN --scope-file FILE | 显式不可变covered IDs、scope/rule/reason | 空stdout、追加声明，不禁止后来Attempt |

契约错误为duplicate_key、schema_invalid、semantic_invalid、missing_parameter、unknown_parameter、parent_mismatch、unauthorized_event、conflicting_event_id、unsupported_major。Registry另有conflict、corrupt_record、registry_unavailable、indeterminate；事件另有spool_unavailable、spool_full、writer_busy，见[spool](../recorder/spool/README.md)。stderr错误机器分类，不暴露秘密，不通过猜Run替代缺失context。indeterminate只可同token重查，不重跑业务。bundle有效退出0、无效1，协议与记录器独立。

离线event额外错误为limit_exceeded、sensitive_field、config_invalid/config_unavailable、input_unavailable、output_unavailable、indeterminate。encode输出只是中间产物，非记录或确认；父引用仍需bundle或context验证。

## 开发入口与证据

本地工具链为WSL2 x86_64、Go1.27.0、Python3.12.3、Bash5.2.21、uv0.11.2、jsonschema4.10.3。Go module为github.com/cemc-oper/sledra/recorder，依赖固定于[go.mod](../recorder/go.mod)/[go.sum](../recorder/go.sum)，窄Python依赖为[requirements-test.txt](requirements-test.txt)。未增加记录器Python运行依赖；发行/平台资格继续单列。

源码根setup准备缓存，check/test离线运行，详见[开发](../docs/source/development/workflow.md)。单项入口仍可用：

```bash
python3 spec/build_contract.py
python3 spec/build_fixtures.py
python3 spec/verify.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
cd recorder && GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-spec-check ./cmd/sledra-spec-check
```

build_contract/build_fixtures会显式重生成提交的JSON，仅在有意更新时运行并审查前后字节；普通check在副本中执行。verify离线构建Go helper，比较每个合成例的分类/成功输出、夹具摘要与golden身份。SLD-02不提交scheduler、执行生产流程、写记录事件、启动Python ingest或证明HPC资格。
