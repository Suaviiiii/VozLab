// VozLab — lançador único do estúdio de voz.
//
// 1. Se o servidor já responde em 127.0.0.1:17493, só abre a janela.
// 2. Senão, liga o servidor do projeto (venv_rocm, mesmas variáveis do lançador
//    antigo) sem janela preta, mostrando "Ligando o VozLab…" enquanto espera.
// 3. Abre a interface nova numa janela própria (Edge/Chrome/Brave em modo app,
//    com perfil separado em %LOCALAPPDATA%\VozLab\janela).
// 4. Ao fechar a janela, desliga o servidor que ELE ligou (libera a VRAM).
//    Se houver geração em andamento, espera terminar ou pergunta.
//
// Compilar: vozlab\compilar_vozlab.bat  (csc do .NET Framework, já vem no Windows)

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows.Forms;

static class VozLab
{
    const int Porta = 17493;
    // Caminhos (achado 5 da varredura): a pasta do programa é a pasta do próprio VozLab.exe.
    // Modelos, OmniVoice, ambiente Python e disfarce da placa podem ser trocados em vozlab.ini (ao lado do .exe):
    //   ModelosDir=D:\Modelos de voz
    //   OmniVoiceDir=D:\OmniVoice
    //   PythonDir=D:\VozLab\venv
    //   HsaOverride=11.0.0      (vazio = automático pela placa; "nenhum" = não usar)
    static readonly string ProjetoDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
    static readonly System.Collections.Generic.Dictionary<string, string> Ini = LerIni(Path.Combine(ProjetoDir, "vozlab.ini"));
    static readonly string VenvDir = Config("PythonDir", Path.Combine(ProjetoDir, "venv_rocm"));
    static readonly string Python = Path.Combine(VenvDir, "Scripts", "python.exe");
    static readonly string ModelosDir = Config("ModelosDir", Path.Combine(ProjetoDir, "modelos"));
    static readonly string OmniVoiceDir = Config("OmniVoiceDir", Path.Combine(ProjetoDir, "OmniVoice"));
    static readonly string DadosDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "sh.voicebox.app");
    static readonly string PerfilJanela = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "VozLab", "janela");
    static readonly string Url = "http://127.0.0.1:" + Porta + "/";
    static string LogPath;
    static readonly object LogLock = new object();

    static Form tela;
    static Label texto;
    static Process servidor;

    [STAThread]
    static int Main()
    {
        string logDir = Path.Combine(DadosDir, "logs");
        try { Directory.CreateDirectory(logDir); } catch { logDir = Path.GetTempPath(); }
        LogPath = Path.Combine(logDir, "vozlab.log");

        bool primeiro;
        using (Mutex unico = new Mutex(true, "VozLab_instancia_unica", out primeiro))
        {
            if (!primeiro)
            {
                // Já existe um VozLab aberto: traz a janela para a frente (ou abre, se não houver).
                if (!TrazerJanela()) AbrirJanela();
                return 0;
            }
            Application.EnableVisualStyles();
            tela = CriarTela();
            Thread t = new Thread(Fluxo);
            t.IsBackground = true;
            tela.Shown += (s, e) => t.Start();
            Application.Run(tela);
            return 0;
        }
    }

    static void Fluxo()
    {
        Log("---- VozLab iniciado ----");
        bool ligadoPorMim = false;
        try
        {
            if (Saudavel(1500))
            {
                Log("Servidor já estava ligado; só abrindo a janela.");
            }
            else
            {
                Mostrar("Ligando o VozLab…\nIsso pode levar até 1 minuto na primeira vez.");
                LigarServidor();
                ligadoPorMim = true;
                DateTime limite = DateTime.Now.AddSeconds(180);
                while (!Saudavel(1500))
                {
                    if (servidor.HasExited)
                    {
                        Falhar("O servidor do VozLab fechou durante a partida (código " + servidor.ExitCode + ").");
                        return;
                    }
                    if (DateTime.Now > limite)
                    {
                        Falhar("O servidor do VozLab não respondeu em 3 minutos.");
                        return;
                    }
                    Thread.Sleep(700);
                }
                Log("Servidor pronto.");
            }

            Process janela = AbrirJanela();
            Esconder();
            if (janela == null)
            {
                // Navegador padrão: não dá para saber quando fecha; o servidor fica ligado.
                Log("Aberto no navegador padrão; o servidor continua ligado.");
                Fechar();
                return;
            }
            Thread.Sleep(3000);
            while (JanelaAberta(janela)) Thread.Sleep(2000);
            Log("Janela fechada.");

            if (ligadoPorMim) DesligarQuandoLivre();
        }
        catch (Exception ex)
        {
            Log("ERRO: " + ex);
            Falhar("Erro inesperado: " + ex.Message);
            return;
        }
        Fechar();
    }

    // ---------------------------------------------------------------- configuração e placa

    static System.Collections.Generic.Dictionary<string, string> LerIni(string caminho)
    {
        var d = new System.Collections.Generic.Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            if (!File.Exists(caminho)) return d;
            foreach (string linha in File.ReadAllLines(caminho, Encoding.UTF8))
            {
                string l = linha.Trim();
                if (l == "" || l.StartsWith("#") || l.StartsWith(";") || l.StartsWith("[")) continue;
                int i = l.IndexOf('=');
                if (i > 0) d[l.Substring(0, i).Trim()] = l.Substring(i + 1).Trim();
            }
        }
        catch { }
        return d;
    }

    static string Config(string chave, string padrao)
    {
        string v;
        return Ini.TryGetValue(chave, out v) && v != "" ? Environment.ExpandEnvironmentVariables(v) : padrao;
    }

    // Nome da placa de vídeo dedicada (AMD ou NVIDIA), pelo Windows. "" se não achar.
    static string NomePlaca()
    {
        try
        {
            using (var busca = new ManagementObjectSearcher("SELECT Name FROM Win32_VideoController"))
                foreach (ManagementObject o in busca.Get())
                {
                    string n = Convert.ToString(o["Name"]) ?? "";
                    string u = n.ToUpper();
                    if (u.Contains("RX ") || u.Contains("NVIDIA") || u.Contains("GEFORCE")) return n;
                }
        }
        catch { }
        return "";
    }

    // Disfarce do ROCm por família (GPU 0): RX 7000 = 11.0.0 (a RX 7600 precisa; as outras aceitam);
    // RX 6000 = 10.3.0; RX 9000, NVIDIA e outras = nenhum. O vozlab.ini (HsaOverride) manda mais.
    static string Disfarce(string placa)
    {
        string v;
        if (Ini.TryGetValue("HsaOverride", out v) && v != "")
            return v.Equals("nenhum", StringComparison.OrdinalIgnoreCase) ? null : v;
        string u = placa.ToUpper();
        if (System.Text.RegularExpressions.Regex.IsMatch(u, @"RX\s*7\d{3}")) return "11.0.0";
        if (System.Text.RegularExpressions.Regex.IsMatch(u, @"RX\s*6\d{3}")) return "10.3.0";
        return null;
    }

    // ---------------------------------------------------------------- servidor

    static void LigarServidor()
    {
        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = Python;
        psi.Arguments = "-u -m backend.server --data-dir \"" + DadosDir + "\" --port " + Porta +
                        " --parent-pid " + Process.GetCurrentProcess().Id;
        psi.WorkingDirectory = ProjetoDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        psi.StandardErrorEncoding = Encoding.UTF8;
        // Mesmas variáveis do lançador anterior (StudioLauncher.cs). Não alterar as do ROCm.
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["VIRTUAL_ENV"] = VenvDir;
        psi.EnvironmentVariables["PATH"] = Path.Combine(VenvDir, "Scripts") + ";" + Environment.GetEnvironmentVariable("PATH");
        string placa = NomePlaca();
        string hsa = Disfarce(placa);
        if (hsa != null) psi.EnvironmentVariables["HSA_OVERRIDE_GFX_VERSION"] = hsa;
        psi.EnvironmentVariables["VOICEBOX_BACKEND_VARIANT"] = placa.ToUpper().Contains("NVIDIA") ? "cuda" : "rocm";
        psi.EnvironmentVariables["OMNIVOICE_DIR"] = OmniVoiceDir;
        Log("Placa: " + (placa == "" ? "não encontrada" : placa) + " | disfarce HSA: " + (hsa ?? "nenhum") +
            " | modelos: " + ModelosDir + " | OmniVoice: " + OmniVoiceDir);
        psi.EnvironmentVariables["PYTHONPATH"] = ProjetoDir;
        psi.EnvironmentVariables["VOICEBOX_MODELS_DIR"] = ModelosDir;
        psi.EnvironmentVariables["HF_HUB_CACHE"] = ModelosDir;
        psi.EnvironmentVariables["HF_HOME"] = ModelosDir;

        servidor = new Process();
        servidor.StartInfo = psi;
        servidor.OutputDataReceived += (s, e) => { if (e.Data != null) Log("[servidor] " + e.Data); };
        servidor.ErrorDataReceived += (s, e) => { if (e.Data != null) Log("[servidor] " + e.Data); };
        servidor.Start();
        servidor.BeginOutputReadLine();
        servidor.BeginErrorReadLine();
        Log("Servidor ligado, PID " + servidor.Id);
    }

    static bool Saudavel(int timeoutMs)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(Url + "health");
            req.Timeout = timeoutMs;
            req.Proxy = null;
            using (HttpWebResponse r = (HttpWebResponse)req.GetResponse())
                return (int)r.StatusCode == 200;
        }
        catch { return false; }
    }

    static bool GeracaoEmAndamento()
    {
        try
        {
            WebClient wc = new WebClient();
            wc.Proxy = null;
            wc.Encoding = Encoding.UTF8;
            string json = wc.DownloadString(Url + "history?limit=20").Replace(" ", "");
            return json.Contains("\"status\":\"generating\"") || json.Contains("\"status\":\"loading_model\"")
                || json.Contains("\"status\":\"pending\"");
        }
        catch { return false; }
    }

    static void DesligarQuandoLivre()
    {
        if (GeracaoEmAndamento())
        {
            Log("Geração em andamento: aguardando antes de desligar.");
            bool agora = false;
            bool reabrir = false;
            tela.Invoke((Action)(() => MostrarEspera(() => agora = true, () => reabrir = true)));
            while (GeracaoEmAndamento() && !agora)
            {
                if (reabrir)
                {
                    reabrir = false;
                    Process j = AbrirJanela();
                    Esconder();
                    Thread.Sleep(3000);
                    while (JanelaAberta(j)) Thread.Sleep(2000);
                    tela.Invoke((Action)(() => tela.Show()));
                }
                Thread.Sleep(3000);
            }
        }
        Mostrar("Desligando o VozLab e liberando a memória da placa de vídeo…");
        try
        {
            WebClient wc = new WebClient();
            wc.Proxy = null;
            wc.UploadString(Url + "shutdown", "POST", "");
        }
        catch { }
        if (servidor != null && !servidor.WaitForExit(15000))
        {
            Log("Servidor não fechou em 15 s; encerrando o processo.");
            try
            {
                Process.Start(new ProcessStartInfo("taskkill", "/T /F /PID " + servidor.Id)
                    { CreateNoWindow = true, UseShellExecute = false }).WaitForExit(10000);
            }
            catch { }
        }
        Log("Servidor desligado.");
    }

    // ------------------------------------------------------------------ janela

    static string Navegador()
    {
        string pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        string pf86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidatos = {
            Path.Combine(pf86, @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(pf, @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(pf, @"Google\Chrome\Application\chrome.exe"),
            Path.Combine(pf86, @"Google\Chrome\Application\chrome.exe"),
            Path.Combine(local, @"Google\Chrome\Application\chrome.exe"),
            Path.Combine(pf, @"BraveSoftware\Brave-Browser\Application\brave.exe"),
        };
        foreach (string c in candidatos) if (File.Exists(c)) return c;
        return null;
    }

    static Process AbrirJanela()
    {
        string nav = Navegador();
        if (nav == null)
        {
            Process.Start(Url);
            return null;
        }
        Directory.CreateDirectory(PerfilJanela);
        ProcessStartInfo psi = new ProcessStartInfo(nav,
            "--app=\"" + Url + "\" --user-data-dir=\"" + PerfilJanela + "\" --no-first-run " +
            "--no-default-browser-check --window-size=1440,900");
        psi.UseShellExecute = false;
        Log("Abrindo janela: " + nav);
        return Process.Start(psi);
    }

    // Processos PRINCIPAIS do navegador com o nosso perfil (os auxiliares têm --type=
    // e alguns, como o crashpad, podem sobrar depois que a janela fecha).
    static System.Collections.Generic.List<int> ProcessosDaJanela()
    {
        var pids = new System.Collections.Generic.List<int>();
        string perfil = PerfilJanela.ToLowerInvariant();
        try
        {
            using (ManagementObjectSearcher q = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='msedge.exe' OR Name='chrome.exe' OR Name='brave.exe'"))
            {
                foreach (ManagementObject p in q.Get())
                {
                    object cl = p["CommandLine"];
                    if (cl == null) continue;
                    string c = cl.ToString().ToLowerInvariant();
                    if (c.Contains(perfil) && !c.Contains("--type=")) pids.Add(Convert.ToInt32(p["ProcessId"]));
                }
            }
        }
        catch (Exception ex) { Log("Aviso ao verificar a janela: " + ex.Message); }
        return pids;
    }

    static bool JanelaAberta(Process inicial)
    {
        try { if (inicial != null && !inicial.HasExited) return true; } catch { }
        return ProcessosDaJanela().Count > 0;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);

    static bool TrazerJanela()
    {
        foreach (int pid in ProcessosDaJanela())
        {
            try
            {
                Process p = Process.GetProcessById(pid);
                IntPtr h = p.MainWindowHandle;
                if (h == IntPtr.Zero) continue;
                if (IsIconic(h)) ShowWindow(h, 9); // SW_RESTORE
                SetForegroundWindow(h);
                Log("Janela já aberta trazida para a frente.");
                return true;
            }
            catch { }
        }
        return false;
    }

    // ---------------------------------------------------------------- tela

    static Form CriarTela()
    {
        Form f = new Form();
        f.Text = "VozLab";
        f.FormBorderStyle = FormBorderStyle.FixedDialog;
        f.MaximizeBox = false;
        f.MinimizeBox = true;
        f.StartPosition = FormStartPosition.CenterScreen;
        f.ClientSize = new Size(420, 150);
        f.BackColor = Color.FromArgb(23, 25, 31);
        f.ForeColor = Color.FromArgb(244, 243, 247);
        f.Font = new Font("Segoe UI", 10f);
        try { f.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        texto = new Label();
        texto.Dock = DockStyle.Fill;
        texto.Padding = new Padding(20);
        texto.TextAlign = ContentAlignment.MiddleCenter;
        texto.Text = "Abrindo o VozLab…";
        f.Controls.Add(texto);
        return f;
    }

    static void MostrarEspera(Action desligarAgora, Action reabrir)
    {
        tela.Controls.Clear();
        tela.ClientSize = new Size(460, 190);
        Label l = new Label();
        l.SetBounds(20, 15, 420, 95);
        l.Text = "Ainda há uma geração em andamento.\nO VozLab vai desligar sozinho quando ela terminar.\n\n" +
                 "Desligar agora interrompe a geração (texto longo pode ser retomado depois).";
        Button b1 = new Button();
        b1.Text = "Reabrir a janela";
        b1.SetBounds(20, 130, 200, 36);
        b1.FlatStyle = FlatStyle.Flat;
        b1.Click += (s, e) => reabrir();
        Button b2 = new Button();
        b2.Text = "Desligar agora";
        b2.SetBounds(240, 130, 200, 36);
        b2.FlatStyle = FlatStyle.Flat;
        b2.ForeColor = Color.FromArgb(255, 140, 140);
        b2.Click += (s, e) => desligarAgora();
        tela.Controls.AddRange(new Control[] { l, b1, b2 });
        texto = l;
        tela.Show();
        tela.Activate();
    }

    static void Mostrar(string msg)
    {
        tela.Invoke((Action)(() => { texto.Text = msg; tela.Show(); }));
    }

    static void Esconder()
    {
        tela.Invoke((Action)(() => tela.Hide()));
    }

    static void Fechar()
    {
        try { tela.Invoke((Action)(() => tela.Close())); } catch { }
    }

    static void Falhar(string msg)
    {
        Log("FALHA: " + msg);
        tela.Invoke((Action)(() =>
        {
            tela.Hide();
            MessageBox.Show(msg + "\n\nDetalhes no registro:\n" + LogPath, "VozLab",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            tela.Close();
        }));
    }

    static void Log(string linha)
    {
        lock (LogLock)
        {
            try { File.AppendAllText(LogPath, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + linha + Environment.NewLine, Encoding.UTF8); }
            catch { }
        }
    }
}
