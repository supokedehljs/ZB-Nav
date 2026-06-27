# ZB-Nav 上传 GitHub 说明

本说明用于以后把 `ZB-Nav` 插件上传或更新到 GitHub 仓库。

## 仓库地址

```text
https://github.com/supokedehljs/ZB-Nav.git
```

## 插件目录

```text
C:\Users\supokede\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\ZB-Nav
```

## 第一次上传

在 PowerShell 中进入插件目录：

```powershell
cd "C:\Users\supokede\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\ZB-Nav"
```

初始化 Git 仓库：

```powershell
git init
```

添加远程 GitHub 仓库：

```powershell
git remote add origin https://github.com/supokedehljs/ZB-Nav.git
```

添加全部文件：

```powershell
git add .
```

创建提交：

```powershell
git commit -m "Initial ZB-Nav addon"
```

把当前分支改名为 `main`：

```powershell
git branch -M main
```

上传到 GitHub：

```powershell
git push -u origin main
```

## 以后更新插件

进入插件目录：

```powershell
cd "C:\Users\supokede\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\ZB-Nav"
```

查看修改状态：

```powershell
git status
```

添加修改：

```powershell
git add .
```

提交修改：

```powershell
git commit -m "Update ZB-Nav addon"
```

推送到 GitHub：

```powershell
git push
```

## 常见问题

### 如果提示没有登录 GitHub

可以使用 GitHub CLI 登录：

```powershell
gh auth login
```

如果没有安装 GitHub CLI，也可以使用浏览器创建 Personal Access Token，然后按 Git 提示输入。

### 如果提示远程仓库已经存在

检查远程地址：

```powershell
git remote -v
```

如果地址不对，修改为正确地址：

```powershell
git remote set-url origin https://github.com/supokedehljs/ZB-Nav.git
```

### 如果提示分支名不是 main

执行：

```powershell
git branch -M main
```

然后再推送：

```powershell
git push -u origin main
```
