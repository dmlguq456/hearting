-- fixture: opencode-v2-choice.sql  (measured: table DDL and user/assistant/text/tool/idle row shapes)
-- Table definitions copied from a fresh OpenCode 2.0.32 opencode.db (no `session`,
-- `message` or `part` table exists there); the foreign key to `project` is dropped.
-- Row shapes follow that build's `session_message` rows: a `user` row with `data.text`,
-- an `assistant` row whose `data.content` holds text and tool parts
-- ({"type":"tool","id","name","state":{"status","input","content","metadata"},"time":{"created"}}),
-- and an `idle` row. The question tool's `state.metadata.answers` is the V2 result
-- metadata the plugin reads (adapters/opencode/plugins/hearting-guards.js), not a
-- recorded answered question. Every string below is invented.
-- Load with sqlite3.Connection.executescript().
CREATE TABLE `session_v2` (`id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text, `slug` text NOT NULL, `directory` text NOT NULL, `title` text, `version` text NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `time_idle` integer);
CREATE TABLE `session_message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `type` text NOT NULL, `seq` integer NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL);
CREATE UNIQUE INDEX `session_message_session_seq_idx` ON `session_message` (`session_id`,`seq`);
INSERT INTO `session_v2` (`id`,`project_id`,`slug`,`directory`,`title`,`version`,`time_created`,`time_updated`) VALUES ('ses_fixturev2000000000000001','global','fixture-v2','/work/fixture-project','fixture v2 session','2.0.32',1790730000000,1790730120000);
INSERT INTO `session_message` VALUES ('msg_v2_01','ses_fixturev2000000000000001','user',4,1790730000000,1790730000000,'{"time":{"created":1790730000000},"text":"v2 정리 시험을 시작합니다","files":[]}');
INSERT INTO `session_message` VALUES ('msg_v2_02','ses_fixturev2000000000000001','assistant',5,1790730005000,1790730040000,'{"time":{"created":1790730005000,"completed":1790730040000},"agent":"build","content":[{"type":"text","text":"v2 질문을 드립니다."},{"type":"tool","id":"call_v2_0001","name":"question","state":{"status":"completed","input":{"questions":[{"header":"저장 방식","question":"v2 저장 방식을 어떻게 할까요?","options":[{"label":"파일로 저장 (권장)","description":"JSON 파일 한 개에 저장합니다."},{"label":"DB로 저장","description":"표준 DB를 씁니다."}]}]},"content":[{"type":"text","text":"User has answered your questions."}],"metadata":{"answers":[["파일로 저장 (권장)"]]}},"time":{"created":1790730006000,"completed":1790730036000}},{"type":"text","text":"v2 파일 저장으로 진행합니다."},{"type":"tool","id":"call_v2_0002","name":"shell","state":{"status":"completed","input":{"command":"echo ok"},"content":[{"type":"text","text":"ok\n"}],"metadata":{"exit":0}},"time":{"created":1790730038000}}],"finish":"tool-calls"}');
INSERT INTO `session_message` VALUES ('msg_v2_03','ses_fixturev2000000000000001','assistant',9,1790730060000,1790730060000,'{"time":{"created":1790730060000},"agent":"build","content":[{"type":"tool","id":"call_v2_0003","name":"question","state":{"status":"running","input":{"questions":[{"header":"방향 확인","question":"v2 이 방향으로 진행할까요?","options":[{"label":"진행 (권장)","description":"그대로 진행"},{"label":"중단","description":"멈춤"}]}]}},"time":{"created":1790730060000}}]}');
INSERT INTO `session_message` VALUES ('msg_v2_04','ses_fixturev2000000000000001','idle',12,1790730061000,1790730061000,'{"time":{"created":1790730061000},"outcome":"succeeded"}');
