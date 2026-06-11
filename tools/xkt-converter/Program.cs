using System.Diagnostics;

namespace ModularXktConverter;

internal sealed record ConverterSpec(string FileName, IReadOnlyList<string> PrefixArgs, string Description);
internal sealed record ConvertJob(string Input, string Output);

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new ConverterForm(args));
    }
}

internal sealed class ConverterForm : Form
{
    private readonly ListBox _inputList = new();
    private readonly TextBox _outputFolder = new();
    private readonly CheckBox _recursive = new() { Text = "하위 폴더까지 변환", AutoSize = true };
    private readonly CheckBox _overwrite = new() { Text = "기존 XKT 덮어쓰기", AutoSize = true };
    private readonly Button _convertButton = new() { Text = "XKT 변환 시작", Height = 42 };
    private readonly Button _openOutputButton = new() { Text = "출력 폴더 열기", Height = 36 };
    private readonly ProgressBar _progress = new() { Height = 10 };
    private readonly Label _status = new() { AutoSize = false, Height = 28, Text = "IFC 파일이나 폴더를 추가하세요." };
    private readonly RichTextBox _log = new()
    {
        BorderStyle = BorderStyle.FixedSingle,
        DetectUrls = true,
        Font = new Font("Consolas", 9F),
        ReadOnly = true,
    };

    private bool _running;
    private string? _lastOutputFolder;

    public ConverterForm(IEnumerable<string> initialInputs)
    {
        Text = "Modular XKT Converter";
        MinimumSize = new Size(900, 620);
        Size = new Size(1040, 720);
        StartPosition = FormStartPosition.CenterScreen;
        AllowDrop = true;

        Font = new Font("Segoe UI", 10F);
        BackColor = Color.FromArgb(244, 248, 252);

        BuildLayout();
        WireEvents();
        AddInputs(initialInputs);
        Log("Ready. IFC 파일 또는 폴더를 창 위로 끌어다 놓을 수 있습니다.");
    }

    private void BuildLayout()
    {
        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(18),
            ColumnCount = 1,
            RowCount = 5,
        };
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 42));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 58));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Controls.Add(root);

        var header = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, AutoSize = true };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var title = new Label
        {
            AutoSize = true,
            Text = "Modular XKT Converter",
            Font = new Font("Segoe UI", 19F, FontStyle.Bold),
            ForeColor = Color.FromArgb(15, 23, 42),
        };
        var subtitle = new Label
        {
            AutoSize = true,
            Text = "IFC 모델을 xeokit 뷰어용 .xkt 파일로 변환합니다.",
            ForeColor = Color.FromArgb(71, 85, 105),
            Padding = new Padding(0, 6, 0, 12),
        };
        var titleStack = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        titleStack.Controls.Add(title);
        titleStack.Controls.Add(subtitle);
        header.Controls.Add(titleStack, 0, 0);
        root.Controls.Add(header, 0, 0);

        var inputsPanel = CreatePanel("입력 IFC", "파일 또는 폴더를 추가하세요. 폴더 입력은 옵션에 따라 하위 폴더까지 검색합니다.");
        inputsPanel.Controls.Add(_inputList);
        _inputList.Dock = DockStyle.Fill;
        _inputList.HorizontalScrollbar = true;
        _inputList.AllowDrop = true;
        root.Controls.Add(inputsPanel, 0, 1);

        var inputButtons = new FlowLayoutPanel { Dock = DockStyle.Fill, Height = 46, FlowDirection = FlowDirection.LeftToRight };
        inputButtons.Controls.Add(MakeButton("IFC 파일 추가", AddFileClicked));
        inputButtons.Controls.Add(MakeButton("폴더 추가", AddFolderClicked));
        inputButtons.Controls.Add(MakeButton("선택 삭제", (_, _) => RemoveSelectedInputs()));
        inputButtons.Controls.Add(MakeButton("목록 비우기", (_, _) => _inputList.Items.Clear()));
        root.Controls.Add(inputButtons, 0, 2);

        var logPanel = CreatePanel("변환 로그", "변환 상태와 오류를 여기에서 확인합니다.");
        logPanel.Controls.Add(_log);
        _log.Dock = DockStyle.Fill;
        root.Controls.Add(logPanel, 0, 3);

        var bottom = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, AutoSize = true };
        bottom.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        bottom.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        bottom.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.Controls.Add(bottom, 0, 4);

        var outputRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, AutoSize = true, Padding = new Padding(0, 10, 0, 6) };
        outputRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        outputRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        outputRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        outputRow.Controls.Add(new Label { Text = "출력 폴더", AutoSize = true, Padding = new Padding(0, 8, 10, 0) }, 0, 0);
        outputRow.Controls.Add(_outputFolder, 1, 0);
        outputRow.Controls.Add(MakeButton("찾기", BrowseOutputClicked), 2, 0);
        _outputFolder.Dock = DockStyle.Fill;
        _outputFolder.PlaceholderText = "비워두면 IFC 파일과 같은 폴더에 .xkt를 생성합니다.";
        bottom.Controls.Add(outputRow);

        var optionRow = new FlowLayoutPanel { Dock = DockStyle.Fill, Height = 36, FlowDirection = FlowDirection.LeftToRight };
        optionRow.Controls.Add(_recursive);
        optionRow.Controls.Add(_overwrite);
        bottom.Controls.Add(optionRow);

        var actionRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4, AutoSize = true };
        actionRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        actionRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        actionRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        actionRow.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        actionRow.Controls.Add(_status, 0, 0);
        actionRow.Controls.Add(_openOutputButton, 1, 0);
        actionRow.Controls.Add(MakeButton("로그 복사", (_, _) => Clipboard.SetText(_log.Text)), 2, 0);
        actionRow.Controls.Add(_convertButton, 3, 0);
        bottom.Controls.Add(actionRow);
        bottom.Controls.Add(_progress);
        _progress.Dock = DockStyle.Fill;

        _convertButton.BackColor = Color.FromArgb(15, 118, 110);
        _convertButton.ForeColor = Color.White;
        _convertButton.FlatStyle = FlatStyle.Flat;
        _convertButton.FlatAppearance.BorderSize = 0;
        _convertButton.Width = 170;
    }

    private static Panel CreatePanel(string title, string description)
    {
        var panel = new Panel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12, 58, 12, 12),
            BackColor = Color.White,
            Margin = new Padding(0, 0, 0, 10),
        };
        panel.Paint += (_, e) =>
        {
            using var border = new Pen(Color.FromArgb(220, 230, 239));
            e.Graphics.DrawRectangle(border, 0, 0, panel.Width - 1, panel.Height - 1);
        };
        panel.Controls.Add(new Label
        {
            Text = title,
            Font = new Font("Segoe UI", 12F, FontStyle.Bold),
            ForeColor = Color.FromArgb(15, 23, 42),
            Location = new Point(12, 10),
            AutoSize = true,
        });
        panel.Controls.Add(new Label
        {
            Text = description,
            ForeColor = Color.FromArgb(71, 85, 105),
            Location = new Point(12, 35),
            AutoSize = true,
        });
        return panel;
    }

    private static Button MakeButton(string text, EventHandler onClick)
    {
        var button = new Button
        {
            Text = text,
            AutoSize = true,
            Height = 34,
            Margin = new Padding(0, 4, 8, 4),
            Padding = new Padding(10, 0, 10, 0),
        };
        button.Click += onClick;
        return button;
    }

    private void WireEvents()
    {
        DragEnter += DragEnterHandler;
        DragDrop += DragDropHandler;
        _inputList.DragEnter += DragEnterHandler;
        _inputList.DragDrop += DragDropHandler;
        _convertButton.Click += async (_, _) => await ConvertClicked();
        _openOutputButton.Click += (_, _) => OpenOutputFolder();
    }

    private void DragEnterHandler(object? sender, DragEventArgs e)
    {
        if (e.Data?.GetDataPresent(DataFormats.FileDrop) == true)
        {
            e.Effect = DragDropEffects.Copy;
        }
    }

    private void DragDropHandler(object? sender, DragEventArgs e)
    {
        if (e.Data?.GetData(DataFormats.FileDrop) is string[] paths)
        {
            AddInputs(paths);
        }
    }

    private void AddFileClicked(object? sender, EventArgs e)
    {
        using var dialog = new OpenFileDialog
        {
            Filter = "IFC files (*.ifc)|*.ifc|All files (*.*)|*.*",
            Multiselect = true,
            Title = "변환할 IFC 파일 선택",
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            AddInputs(dialog.FileNames);
        }
    }

    private void AddFolderClicked(object? sender, EventArgs e)
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "IFC 파일이 들어 있는 폴더 선택",
            UseDescriptionForTitle = true,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            AddInputs([dialog.SelectedPath]);
        }
    }

    private void BrowseOutputClicked(object? sender, EventArgs e)
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "XKT 출력 폴더 선택",
            UseDescriptionForTitle = true,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            _outputFolder.Text = dialog.SelectedPath;
        }
    }

    private void AddInputs(IEnumerable<string> paths)
    {
        foreach (var path in paths.Where(item => !string.IsNullOrWhiteSpace(item)))
        {
            var fullPath = Path.GetFullPath(path);
            if (!_inputList.Items.Cast<string>().Any(item => string.Equals(item, fullPath, StringComparison.OrdinalIgnoreCase)))
            {
                _inputList.Items.Add(fullPath);
            }
        }
        _status.Text = $"{_inputList.Items.Count}개 입력 항목";
    }

    private void RemoveSelectedInputs()
    {
        var selected = _inputList.SelectedItems.Cast<object>().ToList();
        foreach (var item in selected)
        {
            _inputList.Items.Remove(item);
        }
        _status.Text = $"{_inputList.Items.Count}개 입력 항목";
    }

    private async Task ConvertClicked()
    {
        if (_running) return;
        if (_inputList.Items.Count == 0)
        {
            MessageBox.Show(this, "변환할 IFC 파일이나 폴더를 먼저 추가하세요.", "입력 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        var outputFolder = string.IsNullOrWhiteSpace(_outputFolder.Text) ? null : Path.GetFullPath(_outputFolder.Text);
        var jobs = ConverterEngine.BuildJobs(_inputList.Items.Cast<string>(), outputFolder, _recursive.Checked);
        if (jobs.Count == 0)
        {
            MessageBox.Show(this, "변환할 IFC 파일을 찾지 못했습니다.", "IFC 없음", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        SetRunning(true);
        _progress.Minimum = 0;
        _progress.Maximum = jobs.Count;
        _progress.Value = 0;
        _lastOutputFolder = outputFolder ?? Path.GetDirectoryName(jobs[0].Output);
        Log("");
        Log($"변환 작업 {jobs.Count}개를 시작합니다.");
        var converter = ConverterEngine.ResolveConverter();
        Log(converter.Description);

        var failed = 0;
        for (var i = 0; i < jobs.Count; i++)
        {
            var job = jobs[i];
            _status.Text = $"변환 중 {i + 1}/{jobs.Count}: {Path.GetFileName(job.Input)}";
            if (File.Exists(job.Output) && !_overwrite.Checked)
            {
                Log($"SKIP  {job.Output} 이미 존재");
                _progress.Value = i + 1;
                continue;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(job.Output) ?? Directory.GetCurrentDirectory());
            Log($"IFC   {job.Input}");
            Log($"XKT   {job.Output}");
            var result = await ConverterEngine.RunConverterAsync(converter, job.Input, job.Output, Log);
            if (result == 0 && File.Exists(job.Output))
            {
                Log($"OK    {Path.GetFileName(job.Output)}");
            }
            else
            {
                failed += 1;
                Log($"FAIL  {Path.GetFileName(job.Input)}");
            }
            _progress.Value = i + 1;
        }

        _status.Text = failed == 0 ? "변환 완료" : $"변환 완료, 실패 {failed}개";
        SetRunning(false);
        if (failed == 0)
        {
            MessageBox.Show(this, "모든 IFC 변환이 끝났습니다.", "완료", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }

    private void SetRunning(bool running)
    {
        _running = running;
        _convertButton.Enabled = !running;
        _convertButton.Text = running ? "변환 중..." : "XKT 변환 시작";
    }

    private void OpenOutputFolder()
    {
        var folder = _lastOutputFolder;
        if (string.IsNullOrWhiteSpace(folder) && !string.IsNullOrWhiteSpace(_outputFolder.Text))
        {
            folder = _outputFolder.Text;
        }
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            MessageBox.Show(this, "열 수 있는 출력 폴더가 아직 없습니다.", "출력 폴더 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        Process.Start(new ProcessStartInfo("explorer.exe", folder) { UseShellExecute = true });
    }

    private void Log(string message)
    {
        if (InvokeRequired)
        {
            BeginInvoke(new Action<string>(Log), message);
            return;
        }
        _log.AppendText(message + Environment.NewLine);
        _log.ScrollToCaret();
    }
}

internal static class ConverterEngine
{
    private const string ConverterPackage = "@xeokit/xeokit-convert@1.3.2";

    public static List<ConvertJob> BuildJobs(IEnumerable<string> inputs, string? outputFolder, bool recursive)
    {
        var jobs = new List<ConvertJob>();
        foreach (var input in inputs)
        {
            if (Directory.Exists(input))
            {
                var search = recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly;
                foreach (var file in Directory.EnumerateFiles(input, "*.ifc", search))
                {
                    jobs.Add(new ConvertJob(file, OutputFor(file, outputFolder)));
                }
                continue;
            }

            if (!File.Exists(input)) continue;
            if (!string.Equals(Path.GetExtension(input), ".ifc", StringComparison.OrdinalIgnoreCase)) continue;
            jobs.Add(new ConvertJob(input, OutputFor(input, outputFolder)));
        }
        return jobs;
    }

    public static ConverterSpec ResolveConverter()
    {
        var explicitConverter = Environment.GetEnvironmentVariable("MODULAR_ONTOLOGY_XEOKIT_CONVERT_JS");
        if (!string.IsNullOrWhiteSpace(explicitConverter) && File.Exists(explicitConverter))
        {
            return new ConverterSpec("node", [explicitConverter], $"converter: {explicitConverter}");
        }

        foreach (var candidate in ConverterCandidates())
        {
            if (File.Exists(candidate))
            {
                return new ConverterSpec("node", [candidate], $"converter: {candidate}");
            }
        }

        return new ConverterSpec("npx", ["-y", ConverterPackage], $"converter: npx {ConverterPackage}");
    }

    public static async Task<int> RunConverterAsync(ConverterSpec converter, string input, string output, Action<string> log)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = converter.FileName,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };

        foreach (var arg in converter.PrefixArgs)
        {
            startInfo.ArgumentList.Add(arg);
        }
        startInfo.ArgumentList.Add("-s");
        startInfo.ArgumentList.Add(input);
        startInfo.ArgumentList.Add("-f");
        startInfo.ArgumentList.Add("ifc");
        startInfo.ArgumentList.Add("-o");
        startInfo.ArgumentList.Add(output);

        try
        {
            using var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
            process.OutputDataReceived += (_, eventArgs) =>
            {
                if (!string.IsNullOrWhiteSpace(eventArgs.Data)) log(eventArgs.Data);
            };
            process.ErrorDataReceived += (_, eventArgs) =>
            {
                if (!string.IsNullOrWhiteSpace(eventArgs.Data)) log(eventArgs.Data);
            };
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            await process.WaitForExitAsync();
            return process.ExitCode;
        }
        catch (Exception exc)
        {
            log($"converter 실행 실패: {exc.Message}");
            return 1;
        }
    }

    private static string OutputFor(string inputFile, string? outputFolder)
    {
        if (string.IsNullOrWhiteSpace(outputFolder))
        {
            return Path.ChangeExtension(inputFile, ".xkt");
        }
        return Path.Combine(outputFolder, Path.GetFileNameWithoutExtension(inputFile) + ".xkt");
    }

    private static IEnumerable<string> ConverterCandidates()
    {
        var relative = Path.Combine("node_modules", "@xeokit", "xeokit-convert", "convert2xkt.js");
        var roots = new List<string> { Directory.GetCurrentDirectory(), AppContext.BaseDirectory };
        AddParents(roots, Directory.GetCurrentDirectory());
        AddParents(roots, AppContext.BaseDirectory);
        return roots.Distinct(StringComparer.OrdinalIgnoreCase).Select(root => Path.Combine(root, relative));
    }

    private static void AddParents(List<string> roots, string path)
    {
        var current = new DirectoryInfo(path);
        while (current is not null)
        {
            roots.Add(current.FullName);
            current = current.Parent;
        }
    }
}
