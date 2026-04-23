# Sync `values.yaml` to SparrowX-helm

This workflow automatically syncs any change you make in a `values.yaml` file from the **SparrowX-deploy** repo to the **SparrowX-helm** repo.

---

## How to Use

### 1. Clone the Deploy Repository

Open your terminal or VS Code and run:

```bash
git clone https://github.com/MMahad3/SparrowX-deploy.git
cd SparrowX-deploy
```

### 2. Make Your Changes

Edit any `values.yaml` file inside a service folder (for example: `audit-trail/values.yaml`).

You can:

- Update an existing value.
- Add a completely new key.

New keys will be inserted as flat keys (for example: `redis.host: value`).

### 3. Push the Changes

```bash
git add .
git commit -m "Update values.yaml"
git push origin main
```

### 4. Workflow Triggers Automatically

A GitHub Actions workflow will start automatically. It will:

- Detect the changed `values.yaml`.
- Extract the differences.
- Apply them to the Helm repository.
- Create a pull request.

### 5. Open the Helm Repository

Go to:

`https://github.com/MMahad3/SparrowX-helm`

### 6. Find the Generated Branch

Branches are named using the following pattern:

`PLAT/sync-<commit-sha>`

Example:

`PLAT/sync-abc123`

You can find them:

- In the **Branches** tab.
- In the **Pull Requests** tab (a PR is created automatically).

### 7. Verify Your Changes

Open the pull request or switch to the branch, then navigate to:

`chart/values.yaml`

Confirm that:

- Your updated values are present.
- New keys are added correctly.
- Keys appear under the correct service section.

Case-insensitive matching is used.

Example:

`channel-service` matches `ChannelService`, `channelservice`, etc.
