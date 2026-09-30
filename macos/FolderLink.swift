import AppKit
import SwiftUI
import Combine

struct BridgeFailure: LocalizedError {
    var message: String
    var host: String? = nil
    var fingerprint: String? = nil
    var errorDescription: String? { message }
}

@MainActor final class Bridge {
    let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var buffer = Data()
    private var pending: [String: CheckedContinuation<[String: Any], Error>] = [:]
    var ended: (() -> Void)?
    var closing = false

    func launch() throws {
        guard let resources = Bundle.main.resourceURL else { throw BridgeFailure(message: "找不到应用资源。") }
        process.executableURL = resources.appendingPathComponent("engine/folder-link-engine")
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let bytes = handle.availableData
            Task { @MainActor in self?.receive(bytes) }
        }
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self = self else { return }
                self.failAll("同步进程已结束，请重新打开 App。")
                self.ended?()
            }
        }
        try process.run()
    }

    private func receive(_ bytes: Data) {
        if bytes.isEmpty { output.fileHandleForReading.readabilityHandler = nil; return }
        buffer.append(bytes)
        while let newline = buffer.firstIndex(of: 10) {
            let line = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let id = obj["id"] as? String, let continuation = pending.removeValue(forKey: id) else { continue }
            if let error = obj["error"] as? [String: Any] {
                continuation.resume(throwing: BridgeFailure(message: error["message"] as? String ?? "操作失败", host: error["host"] as? String, fingerprint: error["fingerprint"] as? String))
            } else { continuation.resume(returning: obj["result"] as? [String: Any] ?? [:]) }
        }
    }

    private func failAll(_ message: String) {
        let requests = pending.values
        pending.removeAll()
        for continuation in requests { continuation.resume(throwing: BridgeFailure(message: message)) }
    }

    func request(_ method: String, _ data: [String: Any] = [:]) async throws -> [String: Any] {
        guard process.isRunning, !closing else { throw BridgeFailure(message: "同步服务未就绪，请重新打开 App。") }
        let id = UUID().uuidString
        var bytes = try JSONSerialization.data(withJSONObject: ["id": id, "method": method, "data": data])
        bytes.append(10)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            do { try input.fileHandleForWriting.write(contentsOf: bytes) }
            catch { pending.removeValue(forKey: id)?.resume(throwing: error) }
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 40_000_000_000)
                self?.pending.removeValue(forKey: id)?.resume(throwing: BridgeFailure(message: "操作超时，请检查网络后重试。"))
            }
        }
    }

    func shutdown() {
        closing = true
        if process.isRunning {
            try? input.fileHandleForWriting.write(contentsOf: Data("{\"method\":\"shutdown\"}\n".utf8))
            try? input.fileHandleForWriting.close()
        }
    }
}

struct Entry: Decodable { let time: String; let level: String; let message: String }
struct Conflict: Decodable, Identifiable { let path: String; var id: String { path } }
struct SyncStatus: Decodable {
    var running = false
    var mode = "idle"
    var direction = "both"
    var uploaded = 0
    var downloaded = 0
    var skipped = 0
    var last_sync: String? = nil
    var error: String? = nil
    var conflicts: [Conflict] = []
    var logs: [Entry] = []
}
struct TrustRequest: Identifiable { let id = UUID(); let host: String; let fingerprint: String }

struct Profile: Codable, Identifiable {
    var id: UUID
    var host: String
    var port: String
    var username: String
    var local: String
    var remote: String
}
struct RemoteEntry: Decodable, Identifiable {
    let name: String
    let path: String
    let kind: String
    let size: Int64
    var id: String { path }
}

@MainActor final class SyncModel: ObservableObject, Identifiable {
    let id: UUID
    @Published var host: String
    @Published var port: String
    @Published var username: String
    @Published var password = ""
    @Published var local: String
    @Published var remote: String
    @Published var status = SyncStatus()
    @Published var busy = false
    @Published var ready = false
    @Published var closing = false
    @Published var message = ""
    @Published var isError = false
    @Published var trust: TrustRequest?
    @Published var browsing = false
    @Published var browseBusy = false
    @Published var browsePath = ""
    @Published var browseInput = ""
    @Published var browseParent = "/"
    @Published var browseEntries: [RemoteEntry] = []
    @Published var browseError = ""
    @Published var browseValid = false
    private var trusted = ""
    private var pendingAction = "test"
    private var lastAction = "once"
    private var timer: Timer?
    private var polling = false
    let bridge: Bridge
    init(profile: Profile, bridge: Bridge) {
        id = profile.id; host = profile.host; port = profile.port; username = profile.username
        local = profile.local; remote = profile.remote; self.bridge = bridge
    }
    var profile: Profile { Profile(id: id, host: host, port: port, username: username, local: local, remote: remote) }
    var title: String { host.isEmpty ? "新 SSH 连接" : (username.isEmpty ? host : "\(username)@\(host)") }
    var locked: Bool { busy || status.running || trust != nil || browsing || closing || !ready }
    var stateLabel: String {
        if closing { return "正在关闭" }
        if !ready { return "服务未连接" }
        if busy { return "正在连接" }
        if status.error != nil { return "同步已暂停" }
        if !status.conflicts.isEmpty { return "等待处理冲突" }
        if status.running { return status.direction == "pull" ? "正在拉取远程" : status.mode == "auto" ? "自动同步中" : "正在同步" }
        return status.last_sync == nil ? "准备就绪" : "同步已停止"
    }
    var config: [String: Any] {
        ["host": host, "port": port, "username": username, "password": password,
         "local": local, "remote": remote, "trusted": trusted]
    }
    func request(_ method: String, _ data: [String: Any] = [:]) async throws -> [String: Any] {
        if closing { throw BridgeFailure(message: "标签页正在关闭。") }
        var payload = data; payload["_session"] = id.uuidString
        return try await bridge.request(method, payload)
    }
    func launch() {
        ready = bridge.process.isRunning
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
        Task { await refresh() }
    }
    func resetTrust() { trusted = "" }
    func notify(_ text: String, error: Bool = false) { message = text; isError = error }
    func refresh() async {
        guard ready, !polling, !bridge.closing, !closing else { return }
        polling = true
        defer { polling = false }
        do {
            let value = try await request("status")
            status = try JSONDecoder().decode(SyncStatus.self, from: JSONSerialization.data(withJSONObject: value))
        } catch { if !bridge.closing && !closing { notify(error.localizedDescription, error: true) } }
    }
    func perform(_ action: String) {
        guard !busy, !status.running, ready, !closing else { return }
        if !["test", "browse"].contains(action) && (local.isEmpty || remote.isEmpty) { notify("请选择本地文件夹和远程文件夹。", error: true); return }
        pendingAction = action
        busy = true
        notify("正在连接服务器…")
        Task {
            defer { busy = false }
            do {
                let result = try await request("test", config)
                notify("连接成功 · \(result["home"] as? String ?? "SFTP 已就绪")")
                if action == "browse" {
                    browseEntries = []; browsePath = ""; browseInput = remote
                    browseValid = false; browseError = ""; browsing = true
                    await loadRemote(remote)
                } else if action != "test" {
                    var data = config
                    data["automatic"] = action == "auto"
                    data["direction"] = action == "pull" ? "pull" : "both"
                    _ = try await request("start", data)
                    lastAction = action
                    await refresh()
                    notify(action == "pull" ? "正在拉取远程文件；不会上传或删除文件。" : action == "auto" ? "双向同步已开启。切换标签不会中断任务。" : "正在同步两端文件…")
                }
            } catch let error as BridgeFailure {
                if let fingerprint = error.fingerprint {
                    trust = TrustRequest(host: error.host ?? host, fingerprint: fingerprint)
                    notify("首次连接，请核对服务器指纹。")
                } else { notify(error.message, error: true) }
            } catch { notify(error.localizedDescription, error: true) }
        }
    }
    func loadRemote(_ path: String) async {
        guard !browseBusy, !closing else { return }
        browseBusy = true; browseError = ""; browseValid = false
        defer { browseBusy = false }
        do {
            var data = config; data["path"] = path
            let result = try await request("browse", data)
            browsePath = result["path"] as? String ?? "/"
            browseInput = browsePath
            browseParent = result["parent"] as? String ?? "/"
            browseEntries = try JSONDecoder().decode([RemoteEntry].self, from: JSONSerialization.data(withJSONObject: result["entries"] ?? []))
            browseValid = true
        } catch { browseError = error.localizedDescription }
    }
    func selectRemote() { if browseValid { remote = browsePath; browsing = false } }
    func acceptTrust() {
        guard let request = trust else { return }
        trusted = request.fingerprint; trust = nil
        perform(pendingAction)
    }
    func stop() {
        Task {
            do { _ = try await request("stop"); notify("正在停止当前标签，其他任务继续运行…") }
            catch { notify(error.localizedDescription, error: true) }
        }
    }
    func resolve(_ conflict: Conflict, choice: String) {
        Task {
            do {
                _ = try await request("resolve", ["path": conflict.path, "choice": choice])
                notify("已选择保留版本，等待下一轮同步。")
                if !status.running { perform(lastAction == "pull" ? "pull" : "once") }
            } catch { notify(error.localizedDescription, error: true) }
        }
    }
    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择本地文件夹"
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.allowsMultipleSelection = false; panel.prompt = "选择文件夹"
        if !local.isEmpty { panel.directoryURL = URL(fileURLWithPath: local) }
        if panel.runModal() == .OK, let url = panel.url { local = url.path }
    }
    func stopPolling() { timer?.invalidate(); timer = nil }
    func discard() { stopPolling(); password = ""; closing = true }
}

@MainActor final class Workspace: ObservableObject {
    @Published var sessions: [SyncModel] = []
    @Published var selected = UUID()
    let bridge = Bridge()
    private var subscriptions: [UUID: AnyCancellable] = [:]
    init() {
        let defaults = UserDefaults.standard
        let profiles = defaults.data(forKey: "sshProfiles").flatMap { try? JSONDecoder().decode([Profile].self, from: $0) }
        let initial = profiles?.isEmpty == false ? profiles! : [Profile(id: UUID(), host: defaults.string(forKey: "host") ?? "", port: defaults.string(forKey: "port") ?? "22", username: defaults.string(forKey: "username") ?? "", local: defaults.string(forKey: "local") ?? "", remote: defaults.string(forKey: "remote") ?? "")]
        sessions = initial.map { SyncModel(profile: $0, bridge: bridge) }
        selected = defaults.string(forKey: "selectedSSH").flatMap(UUID.init(uuidString:)).flatMap { id in sessions.contains { $0.id == id } ? id : nil } ?? sessions[0].id
        for session in sessions { watch(session) }
    }
    func watch(_ session: SyncModel) {
        subscriptions[session.id] = Publishers.MergeMany([session.$host, session.$port, session.$username, session.$local, session.$remote])
            .dropFirst(5).debounce(for: .milliseconds(300), scheduler: RunLoop.main)
            .sink { [weak self] _ in self?.save() }
    }
    func save() {
        if let bytes = try? JSONEncoder().encode(sessions.map(\.profile)) { UserDefaults.standard.set(bytes, forKey: "sshProfiles") }
        UserDefaults.standard.set(selected.uuidString, forKey: "selectedSSH")
    }
    func select(_ id: UUID) { selected = id; save() }
    func add() {
        let session = SyncModel(profile: Profile(id: UUID(), host: "", port: "22", username: "", local: "", remote: ""), bridge: bridge)
        sessions.append(session); watch(session); selected = session.id
        if bridge.process.isRunning { session.launch() }
        save()
    }
    func close(_ session: SyncModel) {
        guard !session.closing else { return }
        session.closing = true; session.stopPolling()
        Task {
            do {
                _ = try await bridge.request("close", ["_session": session.id.uuidString])
                session.discard()
                sessions.removeAll { $0.id == session.id }; subscriptions.removeValue(forKey: session.id)
                if sessions.isEmpty { add() }
                else if selected == session.id { selected = sessions[0].id }
                save()
            } catch { session.closing = false; session.launch(); session.notify(error.localizedDescription, error: true) }
        }
    }
    func launch() {
        do {
            try bridge.launch()
            bridge.ended = { [weak self] in
                guard let self = self, !self.bridge.closing else { return }
                for session in self.sessions { session.ready = false; session.stopPolling(); session.notify("同步服务已退出，请重新打开 App。", error: true) }
            }
            for session in sessions { session.launch() }
        } catch { for session in sessions { session.notify(error.localizedDescription, error: true) } }
    }
    func shutdown() { save(); for session in sessions { session.discard() }; bridge.shutdown() }
}

struct FrostedWindow: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .underWindowBackground
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

struct GlassCard<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content.padding(22).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(.white.opacity(0.4), lineWidth: 1))
            .shadow(color: .black.opacity(0.035), radius: 16, y: 8)
    }
}

struct Field: View {
    var title: String
    var hint: String
    @Binding var value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            TextField(hint, text: $value).textFieldStyle(.plain).font(.system(size: 13))
                .padding(.horizontal, 12).frame(height: 39)
                .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 9))
                .overlay(RoundedRectangle(cornerRadius: 9).stroke(.primary.opacity(0.06)))
                .accessibilityLabel(title)
        }
    }
}

struct RootView: View {
    @ObservedObject var model: SyncModel
    @ObservedObject var workspace: Workspace
    @State private var showPassword = false
    private let accent = Color(red: 0.16, green: 0.43, blue: 0.65)
    var body: some View {
        ZStack {
            FrostedWindow().ignoresSafeArea()
            GeometryReader { geometry in
                Circle().fill(Color.cyan.opacity(0.10)).frame(width: 460, height: 460).blur(radius: 95).offset(x: geometry.size.width - 330, y: -220)
                Circle().fill(Color.mint.opacity(0.09)).frame(width: 440, height: 440).blur(radius: 100).offset(x: 90, y: geometry.size.height - 240)
            }.allowsHitTesting(false)
            HStack(spacing: 0) {
                sidebar.frame(width: 192)
                Rectangle().fill(.primary.opacity(0.06)).frame(width: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        tabBar
                        heading
                        HStack(alignment: .top, spacing: 16) { connection; mapping }
                        actions
                        if !model.message.isEmpty {
                            Label(model.message, systemImage: model.isError ? "exclamationmark.circle" : "checkmark.circle")
                                .font(.system(size: 11)).foregroundStyle(model.isError ? Color.orange : .secondary)
                                .textSelection(.enabled).padding(.horizontal, 4)
                        }
                        activity
                        HStack {
                            Label("密码仅保留在内存", systemImage: "lock.shield"); Spacer()
                            Text("每轮间隔 3 秒 · 不联动删除")
                        }.font(.system(size: 10)).foregroundStyle(.tertiary).padding(.horizontal, 3)
                    }.padding(28).padding(.top, 18)
                }.scrollIndicators(.hidden)
            }
        }.tint(accent)
        .sheet(item: $model.trust) { request in
            VStack(alignment: .leading, spacing: 18) {
                Image(systemName: "checkmark.shield").font(.system(size: 32)).foregroundStyle(accent)
                Text("确认服务器身份").font(.system(size: 23, weight: .semibold))
                Text("首次连接到 \(request.host)。请核对服务器 SSH 指纹后继续。").font(.system(size: 13)).foregroundStyle(.secondary)
                Text(request.fingerprint).font(.system(size: 12, design: .monospaced)).textSelection(.enabled)
                    .padding(14).frame(maxWidth: .infinity, alignment: .leading).background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                Text("信任后会记住指纹；指纹变化时将阻止连接。").font(.system(size: 11)).foregroundStyle(.secondary)
                HStack { Spacer(); Button("取消") { model.trust = nil }; Button("信任并连接") { model.acceptTrust() }.buttonStyle(.borderedProminent) }
            }.padding(28).frame(width: 440)
        }
        .sheet(isPresented: $model.browsing) { RemoteBrowser(model: model) }
        .onChange(of: model.host) { model.resetTrust() }
        .onChange(of: model.port) { model.resetTrust() }
    }
    var tabBar: some View {
        HStack(spacing: 8) {
            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    ForEach(workspace.sessions) { session in
                        SessionTab(session: session, selected: workspace.selected == session.id,
                                   select: { workspace.select(session.id) }, close: { workspace.close(session) })
                    }
                }
            }.scrollIndicators(.hidden)
            Button { workspace.add() } label: { Image(systemName: "plus").frame(width: 25, height: 25) }
                .buttonStyle(.borderless).help("新建 SSH 标签页（⌘T）").accessibilityLabel("新建 SSH 标签页")
        }
    }
    var sidebar: some View {
        VStack(alignment: .leading, spacing: 26) {
            HStack(spacing: 11) {
                Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 23, weight: .medium))
                    .foregroundStyle(accent).frame(width: 43, height: 43).background(.white.opacity(0.3), in: RoundedRectangle(cornerRadius: 13))
                VStack(alignment: .leading, spacing: 3) { Text("Folder Link").font(.system(size: 16, weight: .semibold)); Text("文件夹，保持连接").font(.system(size: 9)).foregroundStyle(.secondary) }
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("工作空间 · \(workspace.sessions.count) 个连接").font(.system(size: 10, weight: .medium)).foregroundStyle(.tertiary).padding(.leading, 12)
                Label("双向同步", systemImage: "arrow.left.arrow.right").font(.system(size: 12, weight: .medium))
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.white.opacity(0.30), in: RoundedRectangle(cornerRadius: 11))
                    .overlay(RoundedRectangle(cornerRadius: 11).stroke(.white.opacity(0.35)))
            }
            Spacer()
            VStack(alignment: .leading, spacing: 11) {
                Label("本地 ⇄ 远程", systemImage: "point.3.connected.trianglepath.dotted").font(.system(size: 12, weight: .medium))
                Text("两端新增和修改自动同步。\n发生冲突时，由你决定。")
                    .font(.system(size: 10)).foregroundStyle(.secondary).lineSpacing(5)
                Divider().opacity(0.6)
                HStack(spacing: 6) { Circle().fill(model.ready ? .green.opacity(0.7) : .orange).frame(width: 5, height: 5); Text(model.ready ? "同步引擎已就绪" : "正在准备引擎") }.font(.system(size: 10)).foregroundStyle(.secondary)
                Text("macOS · 1.2").font(.system(size: 9)).foregroundStyle(.tertiary)
            }
        }.padding(.horizontal, 20).padding(.top, 52).padding(.bottom, 26)
        .frame(maxHeight: .infinity).background(.white.opacity(0.05))
    }
    var heading: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 9) {
                Text("让文件，自由往返。").font(.system(size: 28, weight: .semibold, design: .rounded)).tracking(-0.8)
                Text("连接你的 Mac 与服务器，修改自动抵达另一端。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Spacer()
            HStack(spacing: 6) {
                if model.busy { ProgressView().controlSize(.mini) }
                else { Circle().fill(model.status.running ? Color.green : accent.opacity(0.6)).frame(width: 6, height: 6) }
                Text(model.stateLabel).font(.system(size: 10, weight: .medium))
            }.padding(.horizontal, 12).padding(.vertical, 9).background(.ultraThinMaterial, in: Capsule())
        }.padding(.bottom, 7)
    }
    func cardTitle(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 8) { Image(systemName: symbol).foregroundStyle(accent); Text(title).fontWeight(.semibold); Spacer(); Text(detail).font(.system(size: 9, weight: .medium)).tracking(1).foregroundStyle(.tertiary) }.font(.system(size: 13))
    }
    var connection: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 17) {
                cardTitle("server.rack", "连接服务器", "SSH")
                HStack(spacing: 10) {
                    Field(title: "服务器 IP / 主机名", hint: "192.168.1.100", value: $model.host)
                    Field(title: "端口", hint: "22", value: $model.port).frame(width: 64)
                }
                Field(title: "SSH 账号", hint: "例如 ubuntu", value: $model.username)
                VStack(alignment: .leading, spacing: 8) {
                    Text("SSH 密码").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                    HStack {
                        Group {
                            if showPassword { TextField("输入登录密码", text: $model.password) }
                            else { SecureField("输入登录密码", text: $model.password) }
                        }.textFieldStyle(.plain).accessibilityLabel("SSH 密码")
                        Button { showPassword.toggle() } label: { Image(systemName: showPassword ? "eye.slash" : "eye").foregroundStyle(.secondary) }.buttonStyle(.plain).help(showPassword ? "隐藏密码" : "显示密码")
                    }.font(.system(size: 13)).padding(.horizontal, 12).frame(height: 39).background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 9)).overlay(RoundedRectangle(cornerRadius: 9).stroke(.primary.opacity(0.06)))
                }
                HStack {
                    Text("SSH 加密传输").font(.system(size: 10)).foregroundStyle(.tertiary)
                    Spacer(); Button("测试连接") { model.perform("test") }.controlSize(.regular)
                }.padding(.top, 3)
            }.disabled(model.locked)
        }.frame(maxWidth: .infinity)
    }
    var mapping: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 17) {
                cardTitle("folder.badge.gearshape", "文件夹映射", "SFTP")
                VStack(alignment: .leading, spacing: 8) {
                    HStack { Label("本地文件夹", systemImage: "laptopcomputer").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary); Spacer(); Text("MAC").font(.system(size: 8)).foregroundStyle(.tertiary) }
                    HStack(spacing: 5) {
                        TextField("选择或输入绝对路径", text: $model.local).textFieldStyle(.plain).font(.system(size: 12)).accessibilityLabel("本地文件夹")
                        Button { model.chooseFolder() } label: { Image(systemName: "folder").font(.system(size: 13)) }.buttonStyle(.plain).help("选择本地文件夹")
                    }.padding(.horizontal, 12).frame(height: 39).background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 9)).overlay(RoundedRectangle(cornerRadius: 9).stroke(.primary.opacity(0.06)))
                }
                HStack(spacing: 10) { Image(systemName: "arrow.up.arrow.down").font(.system(size: 13)); Text("双向自动同步").font(.system(size: 10)); Rectangle().frame(height: 1).opacity(0.15) }.foregroundStyle(accent.opacity(0.75)).padding(.vertical, 1)
                VStack(alignment: .leading, spacing: 8) {
                    HStack { Text("远程文件夹").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary); Spacer(); Button("浏览远程") { model.perform("browse") }.font(.system(size: 10)).buttonStyle(.plain).foregroundStyle(accent) }
                    TextField("/home/ubuntu/project", text: $model.remote).textFieldStyle(.plain).font(.system(size: 12))
                        .padding(.horizontal, 12).frame(height: 39).background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 9))
                        .overlay(RoundedRectangle(cornerRadius: 9).stroke(.primary.opacity(0.06))).accessibilityLabel("远程文件夹")
                }
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "info.circle")
                    Text("两边都修改时提示冲突。\n单端删除会从另一端恢复。")
                        .lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.top, 5)
            }.disabled(model.locked)
        }.frame(maxWidth: .infinity)
    }
    var actions: some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 19)).foregroundStyle(accent)
            VStack(alignment: .leading, spacing: 4) {
                Text(model.status.running ? "文件夹正在保持同步" : "一次连接，持续同步").font(.system(size: 12, weight: .medium))
                Text("各标签独立运行 · 退出停止全部").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Spacer()
            if model.status.running {
                Button("停止同步", systemImage: "stop.fill") { model.stop() }.controlSize(.large)
            } else {
                Button("拉取远程", systemImage: "arrow.down") { model.perform("pull") }.controlSize(.large).disabled(model.locked).help("只下载远程文件，不上传；不同版本会提示冲突")
                Button("同步一次") { model.perform("once") }.controlSize(.large).disabled(model.locked)
                Button { model.perform("auto") } label: { Label("开始自动同步", systemImage: "arrow.left.arrow.right").padding(.horizontal, 4) }
                    .buttonStyle(.borderedProminent).controlSize(.large).disabled(model.locked).keyboardShortcut(.return, modifiers: .command)
            }
        }.padding(18).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).stroke(.white.opacity(0.3)))
    }
    var activity: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 15) {
                HStack {
                    Text("同步动态").font(.system(size: 13, weight: .semibold)); Spacer()
                    Text(model.status.last_sync.map { "上次完成 \($0)" } ?? "等待开始").font(.system(size: 10)).foregroundStyle(.tertiary)
                }
                HStack(spacing: 22) {
                    stat("arrow.up", "已上传", model.status.uploaded)
                    stat("arrow.down", "已下载", model.status.downloaded)
                    stat("checkmark", "未变化", model.status.skipped)
                }
                if !model.status.conflicts.isEmpty {
                    ForEach(model.status.conflicts) { conflict in
                        HStack {
                            Label(conflict.path, systemImage: "exclamationmark.triangle").lineLimit(2).font(.system(size: 11)).foregroundStyle(.orange)
                            Spacer()
                            Button("保留本地") { model.resolve(conflict, choice: "local") }
                            Button("保留远程") { model.resolve(conflict, choice: "remote") }
                        }.controlSize(.small).padding(10).background(.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                    }
                }
                if model.status.logs.isEmpty {
                    HStack(spacing: 13) {
                        Image(systemName: "folder.badge.arrow.forward").font(.system(size: 26, weight: .ultraLight)).foregroundStyle(accent.opacity(0.45))
                        VStack(alignment: .leading, spacing: 5) { Text("准备好连接你的文件夹了").font(.system(size: 12)); Text("填写连接信息和两端路径，然后开始同步。").font(.system(size: 10)).foregroundStyle(.tertiary) }
                    }.foregroundStyle(.secondary).frame(maxWidth: .infinity).frame(height: 74)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 9) {
                            ForEach(Array(model.status.logs.enumerated()), id: \.offset) { _, log in
                                HStack(alignment: .top, spacing: 13) {
                                    Text(log.time).monospacedDigit().foregroundStyle(.tertiary)
                                    Text(log.message).foregroundStyle(log.level == "error" ? Color.orange : log.level == "success" ? accent : Color.secondary).textSelection(.enabled)
                                }.font(.system(size: 10))
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(height: 100)
                }
            }
        }
    }
    func stat(_ symbol: String, _ label: String, _ value: Int) -> some View {
        HStack(spacing: 5) { Image(systemName: symbol); Text(label); Text("\(value)").fontWeight(.semibold).foregroundStyle(.primary) }.font(.system(size: 10)).foregroundStyle(.secondary)
    }
}

struct WorkspaceView: View {
    @ObservedObject var workspace: Workspace
    var body: some View {
        if let session = workspace.sessions.first(where: { $0.id == workspace.selected }) {
            RootView(model: session, workspace: workspace).id(session.id)
        } else { ProgressView().frame(width: 600, height: 400) }
    }
}

struct SessionTab: View {
    @ObservedObject var session: SyncModel
    var selected: Bool
    var select: () -> Void
    var close: () -> Void
    var body: some View {
        HStack(spacing: 10) {
            Button(action: select) {
                HStack(spacing: 7) {
                    Circle().fill(session.status.running ? Color.green : session.status.error != nil ? Color.orange : Color.secondary.opacity(0.35)).frame(width: 6, height: 6)
                    Text(session.title).font(.system(size: 11, weight: selected ? .semibold : .regular)).lineLimit(1)
                }.frame(maxWidth: 190)
            }.buttonStyle(.plain).accessibilityLabel("切换 SSH：\(session.title)")
            if session.closing { ProgressView().controlSize(.mini) }
            else {
                Button(action: close) { Image(systemName: "xmark").font(.system(size: 9, weight: .medium)).foregroundStyle(.secondary) }
                    .buttonStyle(.plain).help("关闭此标签并停止它的同步").accessibilityLabel("关闭 SSH：\(session.title)")
            }
        }.padding(.horizontal, 12).padding(.vertical, 11)
            .background(selected ? .white.opacity(0.43) : .white.opacity(0.13), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(selected ? 0.55 : 0.18)))
    }
}

struct RemoteBrowser: View {
    @ObservedObject var model: SyncModel
    @State private var showHidden = false
    var visible: [RemoteEntry] { model.browseEntries.filter { showHidden || !$0.name.hasPrefix(".") } }
    var body: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack {
                Image(systemName: "folder.badge.gearshape").font(.system(size: 25)).foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 4) {
                    Text("选择远程文件夹").font(.system(size: 20, weight: .semibold))
                    Text(model.title).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                if model.browseBusy { ProgressView().controlSize(.small) }
            }
            HStack(spacing: 9) {
                Button { Task { await model.loadRemote("") } } label: { Image(systemName: "house") }.help("服务器主目录").accessibilityLabel("服务器主目录")
                Button { Task { await model.loadRemote(model.browseParent) } } label: { Image(systemName: "arrow.up") }.disabled(model.browsePath.isEmpty || model.browsePath == "/").help("上一级目录").accessibilityLabel("上一级目录")
                TextField("远程绝对路径", text: $model.browseInput).textFieldStyle(.roundedBorder).onSubmit { Task { await model.loadRemote(model.browseInput) } }
                Button("前往") { Task { await model.loadRemote(model.browseInput) } }
            }.disabled(model.browseBusy)
            if !model.browseError.isEmpty {
                Label(model.browseError, systemImage: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled)
            }
            ScrollView {
                LazyVStack(spacing: 0) {
                    if model.browseEntries.isEmpty && !model.browseBusy && model.browseError.isEmpty {
                        Text("这是一个空文件夹").font(.system(size: 12)).foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.vertical, 75)
                    }
                    ForEach(visible) { entry in
                        Button {
                            if entry.kind == "directory" { Task { await model.loadRemote(entry.path) } }
                        } label: {
                            HStack(spacing: 11) {
                                Image(systemName: entry.kind == "directory" ? "folder.fill" : entry.kind == "link" ? "link" : "doc")
                                    .foregroundStyle(entry.kind == "directory" ? Color.accentColor : .secondary).frame(width: 23)
                                Text(entry.name).lineLimit(1).truncationMode(.middle)
                                Spacer()
                                if entry.kind == "directory" { Image(systemName: "chevron.right").font(.system(size: 9)) }
                                else { Text(entry.kind == "link" ? "符号链接 · 跳过" : ByteCountFormatter.string(fromByteCount: entry.size, countStyle: .file)).font(.system(size: 10)).foregroundStyle(.secondary) }
                            }.font(.system(size: 12)).padding(.horizontal, 12).padding(.vertical, 11).contentShape(Rectangle())
                        }.buttonStyle(.plain).disabled(entry.kind != "directory" || model.browseBusy)
                        Divider().opacity(0.3)
                    }
                }.frame(maxWidth: .infinity)
            }.frame(height: 295).background(.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(.primary.opacity(0.06)))
            HStack {
                Toggle("显示隐藏项目", isOn: $showHidden).font(.system(size: 11)).toggleStyle(.checkbox)
                Spacer()
                Text("\(visible.count) 个项目").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Text(model.browsePath.isEmpty ? "点击文件夹进入，选择当前目录作为同步路径。" : "当前目录：\(model.browsePath)")
                .font(.system(size: 10)).foregroundStyle(.secondary).textSelection(.enabled).lineLimit(2)
            HStack {
                Spacer()
                Button("取消") { model.browsing = false }.keyboardShortcut(.cancelAction)
                Button("选择此文件夹") { model.selectRemote() }.buttonStyle(.borderedProminent)
                    .disabled(model.browseBusy || !model.browseValid || model.browsePath == "/")
            }
        }.padding(25).frame(width: 590)
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let workspace = Workspace()
    var terminating = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于 Folder Link", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "显示主窗口", action: #selector(showWindow), keyEquivalent: "0").target = self
        appMenu.addItem(withTitle: "新建 SSH 标签页", action: #selector(newTab), keyEquivalent: "t").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Folder Link", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem); let editMenu = NSMenu(title: "编辑"); editItem.submenu = editMenu
        for (name, action, key) in [("撤销", Selector(("undo:")), "z"), ("剪切", #selector(NSText.cut(_:)), "x"), ("复制", #selector(NSText.copy(_:)), "c"), ("粘贴", #selector(NSText.paste(_:)), "v"), ("全选", #selector(NSText.selectAll(_:)), "a")] { editMenu.addItem(withTitle: name, action: action, keyEquivalent: key) }
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 880), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "Folder Link"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isOpaque = false; window.backgroundColor = .clear
        window.minSize = NSSize(width: 1120, height: 800)
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: WorkspaceView(workspace: workspace))
        window.setFrameAutosaveName("FolderLinkMainWindow")
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        workspace.launch()
    }
    @objc func newTab() { workspace.add(); showWindow() }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating { return .terminateLater }
        terminating = true
        if !workspace.bridge.process.isRunning { return .terminateNow }
        workspace.bridge.ended = { NSApp.reply(toApplicationShouldTerminate: true) }
        workspace.shutdown()
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 23_000_000_000)
            if workspace.bridge.process.isRunning { workspace.bridge.process.terminate() }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main struct FolderLinkMain {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
