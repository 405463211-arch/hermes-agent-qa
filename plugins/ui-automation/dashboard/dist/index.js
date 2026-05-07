/**
 * UI Automation dashboard tab — lists YAML cases/pages from project-knowledge.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const {
    Card,
    CardHeader,
    CardTitle,
    CardContent,
    Badge,
    Button,
  } = SDK.components;
  const { useState, useEffect } = SDK.hooks;
  const { cn } = SDK.utils;

  function FileTable(props) {
    const { title, rows, empty } = props;
    return React.createElement(Card, null,
      React.createElement(CardHeader, null,
        React.createElement(CardTitle, { className: "text-base" }, title),
        React.createElement(Badge, { variant: "outline" }, rows.length),
      ),
      React.createElement(CardContent, null,
        rows.length === 0
          ? React.createElement("p", {
              className: "text-sm text-muted-foreground",
            }, empty || "（空）")
          : React.createElement("div", {
              className: "overflow-x-auto border border-border rounded-md",
            },
              React.createElement("table", {
                className: "w-full text-left text-sm",
              },
                React.createElement("thead", null,
                  React.createElement("tr", {
                    className: "border-b border-border bg-muted/40",
                  },
                    React.createElement("th", {
                      className: "p-2 font-medium",
                    }, "文件"),
                    React.createElement("th", {
                      className: "p-2 font-medium w-24",
                    }, "大小"),
                  ),
                ),
                React.createElement("tbody", null,
                  rows.map(function (r) {
                    return React.createElement("tr", {
                      key: r.rel_path,
                      className: "border-b border-border/60",
                    },
                      React.createElement("td", {
                        className: "p-2 font-mono text-xs",
                      }, r.rel_path),
                      React.createElement("td", {
                        className: "p-2 text-muted-foreground",
                      }, String(r.bytes)),
                    );
                  }),
                ),
              ),
            ),
      ),
    );
  }

  function UiAutomationPage() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    const [loading, setLoading] = useState(true);

    function load() {
      setLoading(true);
      setErr(null);
      SDK.fetchJSON("/api/plugins/ui_automation/inventory")
        .then(function (d) { setData(d); })
        .catch(function () {
          setErr("无法加载清单（检查 dashboard 是否已重启以挂载插件 API）");
        })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () { load(); }, []);

    return React.createElement("div", { className: "flex flex-col gap-6 max-w-5xl" },
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex items-center gap-3 flex-wrap" },
            React.createElement(CardTitle, { className: "text-lg" }, "UI 自动化资产"),
            React.createElement(Badge, { variant: "outline" }, "project-knowledge"),
          ),
        ),
        React.createElement(CardContent, { className: "flex flex-col gap-3" },
          React.createElement("p", { className: "text-sm text-muted-foreground" },
            "展示 ~/.hermes/project-knowledge/&lt;项目&gt;/automation/ 下的 cases、pages 与 auto-discover 产物。"
            + " 路径可通过环境变量 HERMES_UI_AUTOMATION_ROOT 覆盖。"
          ),
          data && React.createElement("div", {
            className: "rounded-md border border-border bg-muted/30 p-3 font-mono text-xs break-all",
          },
            React.createElement("div", null,
              React.createElement("span", { className: "text-muted-foreground" }, "根目录: "),
              data.automation_root,
            ),
            React.createElement("div", { className: "mt-1 text-muted-foreground" }, data.hint),
          ),
          React.createElement("div", { className: "flex gap-2" },
            React.createElement(Button, {
              onClick: load,
              disabled: loading,
              className: cn(
                "inline-flex items-center gap-2 border border-border bg-background/40 px-4 py-2",
                "text-sm font-courier cursor-pointer",
              ),
            }, loading ? "加载中…" : "刷新"),
          ),
          err && React.createElement("p", {
            className: "text-sm text-destructive",
          }, err),
        ),
      ),

      loading && !data && React.createElement("p", {
        className: "text-sm text-muted-foreground",
      }, "加载中…"),

      data && React.createElement(React.Fragment, null,
        FileTable({
          title: "Cases（cases/*.yaml）",
          rows: data.cases || [],
          empty: "暂无用例；可参考 cases/_template.yaml",
        }),
        FileTable({
          title: "Pages（pages/*.yaml）",
          rows: data.pages || [],
          empty: "暂无页面定义；可参考 pages/_template.yaml",
        }),
        FileTable({
          title: "Auto-discover（pages/auto/*.yaml）",
          rows: data.pages_auto || [],
          empty: "尚无自动入库页面",
        }),
        FileTable({
          title: "Review 图（pages/auto/*_review.png）",
          rows: data.review_images || [],
          empty: "尚无 review 图（触发 auto_discover 后生成）",
        }),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("ui_automation", UiAutomationPage);
})();
