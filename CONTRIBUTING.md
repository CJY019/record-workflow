# 开发、更新与发布

本项目在原有仓库持续更新。普通修改通过分支和 Pull Request（简称 PR，即合并请求）进入 `main`，需要提供下载版本时再创建标签和 GitHub Release。

## 准备测试环境

离线测试使用 Python 3.12，无需显卡、模型权重或真实 API 密钥。

```powershell
uv venv --python 3.12
uv pip install -r requirements-test.txt
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

浏览器测试需要 Node.js 24 和 Chrome、Chromium 或 Edge。程序会启动临时无界面浏览器及模拟服务器，使用合成数据，不访问正在运行的录音应用。

```powershell
node scripts/check_upload_ui.mjs
node scripts/check_math_ui.mjs
```

浏览器安装在其他位置时，将 `CHROME_PATH` 环境变量设为浏览器可执行文件的绝对路径。

## 每次修改的流程

1. 回到主分支并取得最新代码，再创建工作分支。

   ```powershell
   git switch main
   git pull --ff-only
   git switch -c codex/describe-your-change
   ```

2. 修改代码、补充必要测试和更新记录，运行上述检查。

3. 只将需要发布的源码和文档加入暂存区，检查这次提交实际包含的内容。

   ```powershell
   git add app scripts tests .github README.md CHANGELOG.md CONTRIBUTING.md config.yaml .env.example requirements.txt requirements-test.txt cli.py launcher.py
   .venv\Scripts\python.exe scripts/check_public_source.py
   git diff --cached --stat
   git diff --cached
   ```

   暂存区是下一次提交的文件清单。`.env`、`config.local.yaml`、录音、课件、处理结果、缓存、Python 环境和模型目录由 `.gitignore` 排除。检查脚本只报告文件名和行号。

4. 提交并推送工作分支，在 GitHub 页面创建 PR；使用 GitHub CLI 时也可以这样做。

   ```powershell
   git commit -m "Describe the resulting behavior"
   git push -u origin codex/describe-your-change
   gh pr create --base main --fill
   ```

   提交将修改保存到本地 Git 历史；推送将这些提交同步到 GitHub。PR 用来集中审阅本次修改，GitHub Actions 会自动检查源码、离线测试和浏览器行为。

5. 检查通过后合并 PR，回到本地主分支并同步。

   ```powershell
   gh pr checks --watch
   gh pr merge --squash --delete-branch
   git switch main
   git pull --ff-only
   ```

## 创建正式版本

从已通过检查的 `main` 创建版本。修改前写好发布说明，核对提交、标签和配置示例，发布后保留标签所指向的提交。

本项目从 `v0.1.0` 开始记录正式版本。后续小修复可用 `v0.1.1`，一批新功能可用 `v0.2.0`。

```powershell
git switch main
git pull --ff-only
gh release create v0.1.1 --target main --title "v0.1.1" --notes-file RELEASE_NOTES.md
```

这里的版本号和说明文件按实际发布内容填写。GitHub Release 会提供该标签的源码下载。
