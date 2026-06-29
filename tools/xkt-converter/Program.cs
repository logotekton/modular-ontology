using System.Diagnostics;
using System.ComponentModel;
using System.Drawing.Drawing2D;
using System.Text;

namespace ModularOntologyBuilder;

internal sealed record ConverterSpec(string FileName, IReadOnlyList<string> PrefixArgs, string Description);
internal sealed record ConvertJob(string Input, string Output);
internal sealed record ProcessSpec(string FileName, IReadOnlyList<string> Arguments, string WorkingDirectory, string Description);
internal sealed record PackBuildOptions(
    string Mode,
    string SourceFile,
    string OutputFolder,
    string PackName,
    string PackId,
    bool IncludeReadme,
    bool IncludeBackdata,
    bool BuildLocalCrab
);

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new BuilderForm(args));
    }
}

internal static class Ui
{
    public static readonly Color Shell = Color.FromArgb(243, 247, 251);
    public static readonly Color Sidebar = Color.FromArgb(250, 252, 255);
    public static readonly Color Panel = Color.White;
    public static readonly Color PanelAlt = Color.FromArgb(248, 251, 254);
    public static readonly Color Ink = Color.FromArgb(15, 23, 42);
    public static readonly Color Muted = Color.FromArgb(71, 85, 105);
    public static readonly Color Border = Color.FromArgb(214, 225, 234);
    public static readonly Color Accent = Color.FromArgb(15, 118, 110);
    public static readonly Color AccentHover = Color.FromArgb(13, 102, 95);
    public static readonly Color AccentSoft = Color.FromArgb(229, 247, 243);
    public static readonly Color BlueSoft = Color.FromArgb(226, 239, 255);

    public static GraphicsPath Rounded(Rectangle bounds, int radius)
    {
        var path = new GraphicsPath();
        var diameter = radius * 2;
        var arc = new Rectangle(bounds.Location, new Size(diameter, diameter));
        path.AddArc(arc, 180, 90);
        arc.X = bounds.Right - diameter;
        path.AddArc(arc, 270, 90);
        arc.Y = bounds.Bottom - diameter;
        path.AddArc(arc, 0, 90);
        arc.X = bounds.Left;
        path.AddArc(arc, 90, 90);
        path.CloseFigure();
        return path;
    }
}

internal sealed class DoubleBufferedTable : TableLayoutPanel
{
    public DoubleBufferedTable()
    {
        DoubleBuffered = true;
        ResizeRedraw = true;
    }
}

internal sealed class CardPanel : Panel
{
    private readonly DoubleBufferedTable _body = new();
    private bool _hasFillRow;

    public CardPanel(string title, string description)
    {
        DoubleBuffered = true;
        Dock = DockStyle.Fill;
        BackColor = Ui.Panel;
        Padding = new Padding(18);
        Margin = new Padding(0, 0, 14, 14);

        _body.Dock = DockStyle.Fill;
        _body.ColumnCount = 1;
        _body.RowCount = 0;
        _body.GrowStyle = TableLayoutPanelGrowStyle.AddRows;
        _body.BackColor = Color.Transparent;
        Controls.Add(_body);

        AddRow(new Label
        {
            Text = title,
            AutoSize = true,
            Font = new Font("Segoe UI", 12.5F, FontStyle.Bold),
            ForeColor = Ui.Ink,
            Margin = new Padding(0, 0, 0, 2),
        });
        AddRow(new Label
        {
            Text = description,
            AutoSize = true,
            ForeColor = Ui.Muted,
            Margin = new Padding(0, 0, 0, 12),
        });
    }

    public void AddRow(Control control, int bottomMargin = 8)
    {
        control.Dock = DockStyle.Top;
        control.Margin = new Padding(0, 0, 0, bottomMargin);
        _body.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        _body.Controls.Add(control, 0, _body.RowCount);
        _body.RowCount += 1;
    }

    public void AddFill(Control control)
    {
        if (_hasFillRow)
        {
            AddRow(control);
            return;
        }
        _hasFillRow = true;
        control.Dock = DockStyle.Fill;
        control.Margin = new Padding(0, 0, 0, 8);
        _body.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        _body.Controls.Add(control, 0, _body.RowCount);
        _body.RowCount += 1;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var border = new Pen(Ui.Border);
        using var path = Ui.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), 8);
        e.Graphics.DrawPath(border, path);
    }
}

internal sealed class BadgeLabel : Control
{
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public string Status { get; set; } = "Ready";

    public BadgeLabel()
    {
        Width = 96;
        Height = 30;
        DoubleBuffered = true;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var bg = new SolidBrush(Ui.AccentSoft);
        using var path = Ui.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), Height / 2);
        e.Graphics.FillPath(bg, path);

        using var dot = new SolidBrush(Ui.Accent);
        e.Graphics.FillEllipse(dot, 13, 11, 8, 8);
        TextRenderer.DrawText(e.Graphics, Status, Font, new Rectangle(26, 0, Width - 28, Height), Ui.Accent, TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
    }
}

internal sealed class SegmentedToggle : Panel
{
    private readonly Button _revit = new();
    private readonly Button _advance = new();

    public bool IsRevit { get; private set; } = true;
    public event EventHandler? ModeChanged;

    public SegmentedToggle()
    {
        Height = 42;
        BackColor = Color.FromArgb(238, 244, 249);
        Padding = new Padding(3);
        DoubleBuffered = true;

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, BackColor = Color.Transparent };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        Controls.Add(layout);

        Configure(_revit, "Revit IFC");
        Configure(_advance, "Advance Steel XML");
        _revit.Click += (_, _) => SetMode(true);
        _advance.Click += (_, _) => SetMode(false);
        layout.Controls.Add(_revit, 0, 0);
        layout.Controls.Add(_advance, 1, 0);
        RefreshVisuals();
    }

    private static void Configure(Button button, string text)
    {
        button.Text = text;
        button.Dock = DockStyle.Fill;
        button.FlatStyle = FlatStyle.Flat;
        button.FlatAppearance.BorderSize = 0;
        button.Cursor = Cursors.Hand;
        button.Margin = new Padding(0);
    }

    public void SetMode(bool isRevit)
    {
        if (IsRevit == isRevit) return;
        IsRevit = isRevit;
        RefreshVisuals();
        ModeChanged?.Invoke(this, EventArgs.Empty);
    }

    private void RefreshVisuals()
    {
        Style(_revit, IsRevit);
        Style(_advance, !IsRevit);
    }

    private static void Style(Button button, bool active)
    {
        button.BackColor = active ? Color.White : Color.Transparent;
        button.ForeColor = active ? Ui.Accent : Ui.Muted;
        button.Font = new Font("Segoe UI", 9.5F, active ? FontStyle.Bold : FontStyle.Regular);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var border = new Pen(Ui.Border);
        using var path = Ui.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), 8);
        e.Graphics.DrawPath(border, path);
    }
}

internal sealed class DropZonePanel : Panel
{
    private string? _filePath;
    private bool _hover;
    private bool _suppressNextClick;

    public event EventHandler? BrowseRequested;
    public event EventHandler? ClearRequested;

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public string? FilePath
    {
        get => _filePath;
        set
        {
            _filePath = string.IsNullOrWhiteSpace(value) ? null : value;
            Invalidate();
        }
    }

    public DropZonePanel()
    {
        Height = 142;
        Cursor = Cursors.Hand;
        AllowDrop = true;
        DoubleBuffered = true;
        BackColor = Ui.PanelAlt;
    }

    protected override void OnClick(EventArgs e)
    {
        base.OnClick(e);
        if (_suppressNextClick)
        {
            _suppressNextClick = false;
            return;
        }
        BrowseRequested?.Invoke(this, EventArgs.Empty);
    }

    protected override void OnDragEnter(DragEventArgs drgevent)
    {
        base.OnDragEnter(drgevent);
        if (drgevent.Data?.GetDataPresent(DataFormats.FileDrop) == true)
        {
            drgevent.Effect = DragDropEffects.Copy;
            _hover = true;
            Invalidate();
        }
    }

    protected override void OnDragLeave(EventArgs e)
    {
        base.OnDragLeave(e);
        _hover = false;
        Invalidate();
    }

    protected override void OnDragDrop(DragEventArgs drgevent)
    {
        base.OnDragDrop(drgevent);
        _hover = false;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var bounds = new Rectangle(1, 1, Width - 3, Height - 3);
        using var bg = new SolidBrush(_hover ? Ui.AccentSoft : Ui.PanelAlt);
        using var border = new Pen(_hover ? Ui.Accent : Color.FromArgb(190, 207, 222), 1.4F) { DashStyle = DashStyle.Dash };
        using var path = Ui.Rounded(bounds, 8);
        e.Graphics.FillPath(bg, path);
        e.Graphics.DrawPath(border, path);

        if (_filePath is null)
        {
            TextRenderer.DrawText(e.Graphics, "파일을 여기로 끌어다 놓으세요", new Font("Segoe UI", 12F, FontStyle.Bold), new Rectangle(0, 38, Width, 28), Ui.Ink, TextFormatFlags.HorizontalCenter);
            TextRenderer.DrawText(e.Graphics, "또는 클릭하여 파일을 선택합니다", Font, new Rectangle(0, 68, Width, 24), Ui.Muted, TextFormatFlags.HorizontalCenter);
            return;
        }

        var name = Path.GetFileName(_filePath);
        var size = File.Exists(_filePath) ? FileSizeLabel(new FileInfo(_filePath).Length) : "";
        TextRenderer.DrawText(e.Graphics, name, new Font("Segoe UI", 10.5F, FontStyle.Bold), new Rectangle(22, 35, Width - 66, 28), Ui.Ink, TextFormatFlags.EndEllipsis);
        TextRenderer.DrawText(e.Graphics, size, Font, new Rectangle(22, 66, Width - 66, 22), Ui.Muted, TextFormatFlags.EndEllipsis);
        TextRenderer.DrawText(e.Graphics, "x", new Font("Segoe UI", 13F, FontStyle.Bold), new Rectangle(Width - 42, 48, 24, 24), Ui.Muted, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (_filePath is not null && e.X > Width - 52 && e.X < Width - 12 && e.Y > 38 && e.Y < 88)
        {
            _suppressNextClick = true;
            ClearRequested?.Invoke(this, EventArgs.Empty);
            return;
        }
        base.OnMouseDown(e);
    }

    private static string FileSizeLabel(long value)
    {
        if (value >= 1024L * 1024L * 1024L) return $"{value / (1024d * 1024d * 1024d):0.0} GB";
        if (value >= 1024L * 1024L) return $"{value / (1024d * 1024d):0.0} MB";
        if (value >= 1024L) return $"{value / 1024d:0.0} KB";
        return $"{value} B";
    }
}

internal sealed class PipelineStrip : FlowLayoutPanel
{
    private readonly List<Label> _steps = [];
    private int _activeStep;

    public PipelineStrip(params string[] steps)
    {
        AutoSize = true;
        Dock = DockStyle.Top;
        FlowDirection = FlowDirection.LeftToRight;
        WrapContents = true;
        Margin = new Padding(0, 8, 0, 8);
        foreach (var step in steps)
        {
            var label = new Label
            {
                AutoSize = true,
                Text = step,
                Padding = new Padding(10, 7, 10, 7),
                Margin = new Padding(0, 0, 8, 6),
            };
            _steps.Add(label);
            Controls.Add(label);
        }
        SetStep(0);
    }

    public void SetStep(int index)
    {
        _activeStep = Math.Max(0, Math.Min(index, _steps.Count - 1));
        for (var i = 0; i < _steps.Count; i++)
        {
            var done = i < _activeStep;
            var active = i == _activeStep;
            _steps[i].Text = $"{(done ? "✓" : i + 1)}. {_steps[i].Text.Split(". ", 2).Last()}";
            _steps[i].BackColor = active ? Ui.AccentSoft : done ? Color.FromArgb(236, 253, 245) : Ui.PanelAlt;
            _steps[i].ForeColor = active || done ? Ui.Accent : Ui.Muted;
            _steps[i].Font = new Font("Segoe UI", 9F, active ? FontStyle.Bold : FontStyle.Regular);
        }
    }
}

internal sealed class SlimProgressBar : Control
{
    private int _minimum;
    private int _maximum = 100;
    private int _value;

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public int Minimum
    {
        get => _minimum;
        set
        {
            _minimum = value;
            if (_maximum < _minimum) _maximum = _minimum;
            Value = _value;
        }
    }

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public int Maximum
    {
        get => _maximum;
        set
        {
            _maximum = Math.Max(value, _minimum);
            Value = _value;
        }
    }

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public int Value
    {
        get => _value;
        set
        {
            _value = Math.Max(_minimum, Math.Min(value, _maximum));
            Invalidate();
        }
    }

    public SlimProgressBar()
    {
        Height = 6;
        Dock = DockStyle.Top;
        DoubleBuffered = true;
        Margin = new Padding(0, 2, 0, 10);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var bounds = new Rectangle(0, 0, Width - 1, Height - 1);
        using var track = new SolidBrush(Color.FromArgb(226, 234, 242));
        using var trackPath = Ui.Rounded(bounds, Height / 2);
        e.Graphics.FillPath(track, trackPath);
        if (_maximum <= _minimum) return;
        var ratio = (double)(_value - _minimum) / (_maximum - _minimum);
        var fillWidth = Math.Max(0, (int)Math.Round(bounds.Width * ratio));
        if (fillWidth <= 0) return;
        using var fill = new SolidBrush(Ui.Accent);
        using var fillPath = Ui.Rounded(new Rectangle(0, 0, fillWidth, Height - 1), Height / 2);
        e.Graphics.FillPath(fill, fillPath);
    }
}

internal sealed class BuilderForm : Form
{
    private readonly Dictionary<string, Button> _navButtons = [];
    private readonly Dictionary<string, Control> _pages = [];
    private readonly BadgeLabel _statusBadge = new();
    private readonly Label _bottomStatus = new()
    {
        AutoSize = false,
        Height = 24,
        Dock = DockStyle.Fill,
        ForeColor = Ui.Muted,
        TextAlign = ContentAlignment.MiddleLeft,
    };
    private readonly SlimProgressBar _progress = new();
    private readonly RichTextBox _log = new()
    {
        BorderStyle = BorderStyle.None,
        DetectUrls = true,
        Font = new Font("Consolas", 9F),
        ReadOnly = true,
        BackColor = Ui.PanelAlt,
        WordWrap = false,
        ScrollBars = RichTextBoxScrollBars.Vertical,
    };
    private readonly ListBox _historyList = new() { BorderStyle = BorderStyle.None, BackColor = Ui.PanelAlt };

    private readonly DropZonePanel _xktDropZone = new();
    private readonly ListBox _xktInputs = new() { HorizontalScrollbar = true, BorderStyle = BorderStyle.None, BackColor = Ui.PanelAlt };
    private readonly TextBox _xktOutput = new();
    private readonly CheckBox _xktRecursive = new() { Text = "하위 폴더까지 검색", AutoSize = true };
    private readonly CheckBox _xktOverwrite = new() { Text = "기존 XKT 덮어쓰기", AutoSize = true };
    private PipelineStrip _xktPipeline = null!;

    private readonly SegmentedToggle _modeToggle = new();
    private readonly DropZonePanel _packDropZone = new();
    private readonly TextBox _packSource = new() { Visible = false };
    private readonly TextBox _packName = new() { Text = "새 온톨로지팩" };
    private readonly TextBox _packId = new();
    private readonly TextBox _packOutput = new();
    private readonly CheckBox _includeReadme = new() { Text = "README 포함", AutoSize = true, Checked = true };
    private readonly CheckBox _includeBackdata = new() { Text = "Backdata 포함", AutoSize = true, Checked = true };
    private readonly CheckBox _buildLocalCrab = new() { Text = "LocalCrab 호환 ZIP도 생성", AutoSize = true };
    private PipelineStrip _packPipeline = null!;

    private bool _running;
    private string _activePage = "pack";
    private string? _lastOutputFolder;

    public BuilderForm(IEnumerable<string> initialInputs)
    {
        SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
        Text = "Modular Ontology Builder";
        MinimumSize = new Size(1180, 760);
        Size = new Size(1440, 900);
        StartPosition = FormStartPosition.CenterScreen;
        AllowDrop = true;
        Font = new Font("Segoe UI", 10F);
        BackColor = Ui.Shell;

        BuildShell();
        WireEvents();
        LoadHistory();
        AddInitialInputs(initialInputs);
        SetStatus("Ready");
        SetBottomStatus("Ready. IFC, XML, 또는 폴더를 창 위로 끌어다 놓을 수 있습니다.");
    }

    private void BuildShell()
    {
        var shell = new DoubleBufferedTable
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 1,
            BackColor = Ui.Shell,
        };
        shell.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 236));
        shell.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        Controls.Add(shell);

        shell.Controls.Add(BuildSidebar(), 0, 0);

        var main = new DoubleBufferedTable
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(24, 22, 24, 18),
            RowCount = 4,
            ColumnCount = 1,
        };
        main.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        main.RowStyles.Add(new RowStyle(SizeType.Percent, 58));
        main.RowStyles.Add(new RowStyle(SizeType.Percent, 42));
        main.RowStyles.Add(new RowStyle(SizeType.Absolute, 24));
        shell.Controls.Add(main, 1, 0);

        main.Controls.Add(BuildHeader(), 0, 0);

        var contentHost = new Panel { Dock = DockStyle.Fill, Margin = new Padding(0, 14, 0, 8) };
        main.Controls.Add(contentHost, 0, 1);
        _pages["xkt"] = BuildXktPage();
        _pages["pack"] = BuildPackPage();
        _pages["history"] = BuildHistoryPage();
        foreach (var page in _pages.Values)
        {
            page.Dock = DockStyle.Fill;
            contentHost.Controls.Add(page);
        }

        main.Controls.Add(BuildLogPanel(), 0, 2);
        main.Controls.Add(_bottomStatus, 0, 3);
        ShowPage("pack");
    }

    private Control BuildSidebar()
    {
        var sidebar = new DoubleBufferedTable
        {
            Dock = DockStyle.Fill,
            BackColor = Ui.Sidebar,
            Padding = new Padding(18, 24, 18, 18),
            RowCount = 4,
        };
        sidebar.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        sidebar.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        sidebar.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        sidebar.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        sidebar.Paint += (_, e) =>
        {
            using var pen = new Pen(Ui.Border);
            e.Graphics.DrawLine(pen, sidebar.Width - 1, 0, sidebar.Width - 1, sidebar.Height);
        };

        sidebar.Controls.Add(new Label
        {
            Text = "Modular Ontology\nBuilder",
            Font = new Font("Segoe UI", 16F, FontStyle.Bold),
            ForeColor = Ui.Ink,
            AutoSize = true,
            Padding = new Padding(0, 0, 0, 22),
        }, 0, 0);

        var nav = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            AutoSize = true,
        };
        AddNavButton(nav, "xkt", "XKT 변환");
        AddNavButton(nav, "pack", "팩 빌더");
        AddNavButton(nav, "history", "작업 기록");
        sidebar.Controls.Add(nav, 0, 1);

        var footer = new Label
        {
            Text = "하네스 실행\n로그와 산출물을 기록합니다",
            AutoSize = false,
            Height = 62,
            Dock = DockStyle.Fill,
            ForeColor = Color.FromArgb(14, 116, 144),
            BackColor = Color.FromArgb(236, 253, 245),
            Padding = new Padding(12),
            TextAlign = ContentAlignment.MiddleLeft,
        };
        sidebar.Controls.Add(footer, 0, 3);
        return sidebar;
    }

    private void AddNavButton(Control parent, string key, string text)
    {
        var button = new Button
        {
            Text = text,
            Width = 196,
            Height = 42,
            FlatStyle = FlatStyle.Flat,
            TextAlign = ContentAlignment.MiddleLeft,
            Padding = new Padding(14, 0, 0, 0),
            Margin = new Padding(0, 0, 0, 8),
            Cursor = Cursors.Hand,
        };
        button.FlatAppearance.BorderSize = 0;
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(245, 249, 252);
        button.Click += (_, _) => ShowPage(key);
        _navButtons[key] = button;
        parent.Controls.Add(button);
    }

    private Control BuildHeader()
    {
        var header = new DoubleBufferedTable { Dock = DockStyle.Fill, ColumnCount = 2, AutoSize = true };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

        var titleStack = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, WrapContents = false, AutoSize = true };
        titleStack.Controls.Add(new Label
        {
            Text = "Modular Ontology Builder",
            Font = new Font("Segoe UI", 22F, FontStyle.Bold),
            ForeColor = Ui.Ink,
            AutoSize = true,
        });
        titleStack.Controls.Add(new Label
        {
            Text = "IFC/XKT 변환과 OpenCrab 온톨로지 팩 생성을 하나의 하네스로 실행합니다.",
            ForeColor = Ui.Muted,
            AutoSize = true,
            Padding = new Padding(0, 4, 0, 0),
        });
        header.Controls.Add(titleStack, 0, 0);
        header.Controls.Add(_statusBadge, 1, 0);
        return header;
    }

    private Control BuildPackPage()
    {
        var grid = new DoubleBufferedTable { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42));
        grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var left = new CardPanel("온톨로지 팩 생성", "Revit IFC 또는 Advance Steel XML을 업로드용 ZIP 팩으로 변환합니다.");
        left.AddRow(_modeToggle, 12);
        left.AddFill(_packDropZone);
        left.AddRow(LabeledControl("팩 이름", _packName));
        left.AddRow(LabeledControl("팩 ID", _packId));
        grid.Controls.Add(left, 0, 0);

        var right = new CardPanel("출력 설정", "분석, 문서화, JSONL 생성, ZIP 패키징을 순서대로 실행합니다.");
        right.AddRow(LabeledControl("출력 폴더", _packOutput));
        right.AddRow(ButtonRow(MakeButton("파일 선택", SelectPackSourceClicked), MakeButton("출력 폴더 선택", BrowsePackOutputClicked)));
        right.AddRow(ButtonRow(_includeReadme, _includeBackdata));
        right.AddRow(_buildLocalCrab);
        _packPipeline = new PipelineStrip("원본 분석", "모듈/객체 추출", "문서/JSONL 생성", "ZIP 패키징");
        right.AddRow(_packPipeline, 18);
        right.AddRow(ButtonRow(ActionButton("출력 폴더 열기", (_, _) => OpenOutputFolder()), PrimaryActionButton("팩 생성 시작", async (_, _) => await BuildPackClicked())));
        grid.Controls.Add(right, 1, 0);

        return grid;
    }

    private Control BuildXktPage()
    {
        var grid = new DoubleBufferedTable { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42));
        grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var left = new CardPanel("IFC 모델", "IFC 파일 또는 폴더를 추가하세요. 폴더는 옵션에 따라 하위 폴더까지 검색합니다.");
        left.AddRow(_xktDropZone, 12);
        left.AddFill(_xktInputs);
        left.AddRow(ButtonRow(
            MakeButton("IFC 파일 추가", AddIfcFilesClicked),
            MakeButton("폴더 추가", AddIfcFolderClicked),
            MakeButton("선택 삭제", (_, _) =>
            {
                RemoveSelected(_xktInputs);
                RefreshXktInputState();
            }),
            MakeButton("목록 비우기", (_, _) => ClearXktInputs())
        ));
        grid.Controls.Add(left, 0, 0);

        var right = new CardPanel("출력 설정", "Google Drive 업로드 전 xeokit 뷰어용 .xkt를 생성합니다.");
        right.AddRow(LabeledControl("출력 폴더", _xktOutput));
        right.AddRow(ButtonRow(_xktRecursive, _xktOverwrite));
        _xktPipeline = new PipelineStrip("입력 수집", "변환기 확인", "XKT 생성", "산출물 기록");
        right.AddRow(_xktPipeline, 18);
        right.AddRow(ButtonRow(ActionButton("출력 폴더 열기", (_, _) => OpenOutputFolder()), PrimaryActionButton("XKT 변환 시작", async (_, _) => await ConvertXktClicked())));
        grid.Controls.Add(right, 1, 0);
        return grid;
    }

    private Control BuildHistoryPage()
    {
        var panel = new CardPanel("작업 기록", "완료된 변환과 팩 생성 작업을 확인합니다.");
        panel.AddFill(_historyList);
        panel.AddRow(ButtonRow(MakeButton("기록 새로고침", (_, _) => LoadHistory()), MakeButton("기록 파일 열기", (_, _) => OpenHistoryFile())));
        return panel;
    }

    private Control BuildLogPanel()
    {
        var panel = new CardPanel("작업 로그", "실행 상태와 오류를 여기에서 확인합니다.");
        var tools = ButtonRow(MakeButton("로그 복사", (_, _) => CopyLog()), MakeButton("로그 지우기", (_, _) => _log.Clear()));
        tools.FlowDirection = FlowDirection.RightToLeft;
        panel.AddRow(tools, 2);
        _progress.Visible = false;
        panel.AddRow(_progress, 10);
        panel.AddFill(BuildLogSurface());
        return panel;
    }

    private Control BuildLogSurface()
    {
        var logBackground = Color.FromArgb(241, 246, 250);
        var surface = new Panel
        {
            Dock = DockStyle.Fill,
            BackColor = logBackground,
            BorderStyle = BorderStyle.FixedSingle,
            Padding = new Padding(10),
            Margin = new Padding(0, 0, 0, 8),
        };
        _log.Dock = DockStyle.Fill;
        _log.Margin = new Padding(0);
        _log.BackColor = logBackground;
        surface.Controls.Add(_log);
        return surface;
    }

    private static Control LabeledControl(string label, TextBox input)
    {
        input.BorderStyle = BorderStyle.FixedSingle;
        input.Height = 30;
        var layout = new DoubleBufferedTable { Dock = DockStyle.Top, ColumnCount = 1, Height = 58, Margin = new Padding(0, 0, 0, 8) };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.Controls.Add(new Label { Text = label, AutoSize = true, Font = new Font("Segoe UI", 9F, FontStyle.Bold), ForeColor = Ui.Ink, Margin = new Padding(0, 0, 0, 5) }, 0, 0);
        layout.Controls.Add(input, 0, 1);
        input.Dock = DockStyle.Fill;
        return layout;
    }

    private static FlowLayoutPanel ButtonRow(params Control[] controls)
    {
        var row = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = true,
            Margin = new Padding(0, 4, 0, 8),
        };
        row.Controls.AddRange(controls);
        return row;
    }

    private static Button MakeButton(string text, EventHandler onClick)
    {
        var button = new Button
        {
            Text = text,
            AutoSize = true,
            Height = 36,
            Margin = new Padding(0, 4, 8, 4),
            Padding = new Padding(12, 0, 12, 0),
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.White,
            Cursor = Cursors.Hand,
        };
        button.FlatAppearance.BorderColor = Ui.Border;
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(243, 248, 252);
        button.Click += onClick;
        return button;
    }

    private static Button PrimaryButton(string text, EventHandler onClick)
    {
        var button = MakeButton(text, onClick);
        button.Height = 42;
        button.BackColor = Ui.Accent;
        button.ForeColor = Color.White;
        button.FlatAppearance.BorderSize = 0;
        button.FlatAppearance.MouseOverBackColor = Ui.AccentHover;
        return button;
    }

    private static Button ActionButton(string text, EventHandler onClick)
    {
        var button = MakeButton(text, onClick);
        button.AutoSize = false;
        button.Width = 164;
        button.Height = 40;
        return button;
    }

    private static Button PrimaryActionButton(string text, EventHandler onClick)
    {
        var button = PrimaryButton(text, onClick);
        button.AutoSize = false;
        button.Width = 164;
        button.Height = 40;
        return button;
    }

    private void WireEvents()
    {
        DragEnter += DragEnterHandler;
        DragDrop += DragDropHandler;
        _xktInputs.DragEnter += DragEnterHandler;
        _xktInputs.DragDrop += DragDropHandler;
        _xktDropZone.DragDrop += DragDropHandler;
        _packDropZone.DragDrop += DragDropHandler;
        _xktDropZone.BrowseRequested += AddIfcFilesClicked;
        _xktDropZone.ClearRequested += (_, _) => ClearXktInputs();
        _packDropZone.BrowseRequested += SelectPackSourceClicked;
        _packDropZone.ClearRequested += (_, _) => SetPackSource(null);
        _modeToggle.ModeChanged += (_, _) => SyncPackMode();
        SyncPackMode();
    }

    private void ShowPage(string key)
    {
        _activePage = key;
        foreach (var (pageKey, page) in _pages)
        {
            page.Visible = pageKey == key;
        }
        foreach (var (buttonKey, button) in _navButtons)
        {
            var active = buttonKey == key;
            button.BackColor = active ? Ui.BlueSoft : Color.Transparent;
            button.ForeColor = active ? Color.FromArgb(7, 89, 133) : Ui.Ink;
            button.Font = new Font("Segoe UI", 9.5F, active ? FontStyle.Bold : FontStyle.Regular);
        }
    }

    private void SetRunning(bool running)
    {
        _running = running;
        UseWaitCursor = running;
        _progress.Visible = running;
        if (running) SetStatus("Running");
    }

    private void SetStatus(string value)
    {
        _statusBadge.Status = value;
        _statusBadge.Invalidate();
    }

    private void SetBottomStatus(string value)
    {
        _bottomStatus.Text = value;
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

    private void RunOnUi(Action action)
    {
        if (InvokeRequired)
        {
            BeginInvoke(action);
            return;
        }
        action();
    }

    private void CopyLog()
    {
        if (string.IsNullOrEmpty(_log.Text))
        {
            SetBottomStatus("복사할 로그가 없습니다.");
            return;
        }
        try
        {
            Clipboard.SetText(_log.Text);
        }
        catch (Exception exc)
        {
            Log($"클립보드 복사 실패: {exc.Message}");
        }
    }

    private void AddInitialInputs(IEnumerable<string> inputs)
    {
        var paths = inputs.Where(item => !string.IsNullOrWhiteSpace(item)).ToList();
        if (paths.Count > 0) AddDroppedPaths(paths);
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
            AddDroppedPaths(paths);
        }
    }

    private void AddDroppedPaths(IEnumerable<string> paths)
    {
        var list = paths.Where(item => !string.IsNullOrWhiteSpace(item)).ToList();
        if (_activePage == "pack")
        {
            var file = list.FirstOrDefault(File.Exists);
            if (file is not null) SetPackSource(Path.GetFullPath(file));
            return;
        }
        if (_activePage != "xkt")
        {
            SetBottomStatus("파일을 추가하려면 XKT 변환 또는 팩 빌더 탭을 선택하세요.");
            return;
        }
        AddXktInputs(list);
    }

    private void AddIfcFilesClicked(object? sender, EventArgs e)
    {
        using var dialog = new OpenFileDialog
        {
            Filter = "IFC files (*.ifc)|*.ifc|All files (*.*)|*.*",
            Multiselect = true,
            Title = "변환할 IFC 파일 선택",
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            AddXktInputs(dialog.FileNames);
        }
    }

    private void AddIfcFolderClicked(object? sender, EventArgs e)
    {
        using var dialog = new FolderBrowserDialog { Description = "IFC 파일이 들어 있는 폴더 선택", UseDescriptionForTitle = true };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            AddXktInputs([dialog.SelectedPath]);
        }
    }

    private void AddXktInputs(IEnumerable<string> paths)
    {
        foreach (var path in paths)
        {
            var fullPath = Path.GetFullPath(path);
            if (!_xktInputs.Items.Cast<string>().Any(item => string.Equals(item, fullPath, StringComparison.OrdinalIgnoreCase)))
            {
                _xktInputs.Items.Add(fullPath);
            }
        }
        RefreshXktInputState();
    }

    private void ClearXktInputs()
    {
        _xktInputs.Items.Clear();
        RefreshXktInputState();
    }

    private void RefreshXktInputState()
    {
        _xktDropZone.FilePath = _xktInputs.Items.Count == 0 ? null : $"{_xktInputs.Items.Count}개 입력 항목";
        SetStatus(_xktInputs.Items.Count == 0 ? "Ready" : $"{_xktInputs.Items.Count} inputs");
        SetBottomStatus(_xktInputs.Items.Count == 0 ? "Ready. IFC, XML, 또는 폴더를 창 위로 끌어다 놓을 수 있습니다." : $"{_xktInputs.Items.Count}개 입력 항목이 준비되었습니다.");
    }

    private static void RemoveSelected(ListBox list)
    {
        var selected = list.SelectedItems.Cast<object>().ToList();
        foreach (var item in selected)
        {
            list.Items.Remove(item);
        }
    }

    private async Task ConvertXktClicked()
    {
        if (_running) return;
        if (_xktInputs.Items.Count == 0)
        {
            MessageBox.Show(this, "변환할 IFC 파일이나 폴더를 먼저 추가하세요.", "입력 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        var outputFolder = string.IsNullOrWhiteSpace(_xktOutput.Text) ? null : Path.GetFullPath(_xktOutput.Text);
        var jobs = ConverterEngine.BuildJobs(_xktInputs.Items.Cast<string>(), outputFolder, _xktRecursive.Checked);
        if (jobs.Count == 0)
        {
            MessageBox.Show(this, "변환할 IFC 파일을 찾지 못했습니다.", "IFC 없음", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        var failed = 0;
        try
        {
            SetRunning(true);
            _xktPipeline.SetStep(0);
            _progress.Minimum = 0;
            _progress.Maximum = jobs.Count;
            _progress.Value = 0;
            _lastOutputFolder = outputFolder ?? Path.GetDirectoryName(jobs[0].Output);
            Log("");
            Log($"XKT 변환 작업 {jobs.Count}개를 시작합니다.");
            _xktPipeline.SetStep(1);
            var converter = ConverterEngine.ResolveConverter();
            Log(converter.Description);

            for (var i = 0; i < jobs.Count; i++)
            {
                _xktPipeline.SetStep(2);
                var job = jobs[i];
                if (File.Exists(job.Output) && !_xktOverwrite.Checked)
                {
                    Log($"SKIP  {job.Output} 이미 존재");
                    _progress.Value = i + 1;
                    continue;
                }

                Directory.CreateDirectory(Path.GetDirectoryName(job.Output) ?? Directory.GetCurrentDirectory());
                Log($"IFC   {job.Input}");
                Log($"XKT   {job.Output}");
                var result = await ProcessRunner.RunAsync(ConverterEngine.ToProcess(converter, job.Input, job.Output), Log);
                if (result == 0 && File.Exists(job.Output))
                {
                    Log($"OK    {Path.GetFileName(job.Output)}");
                    AddHistory($"XKT 변환 완료: {job.Output}");
                }
                else
                {
                    failed += 1;
                    Log($"FAIL  {Path.GetFileName(job.Input)}");
                }
                _progress.Value = i + 1;
            }

            _xktPipeline.SetStep(3);
            SetStatus(failed == 0 ? "Complete" : $"Failed {failed}");
            if (failed == 0)
            {
                MessageBox.Show(this, "모든 IFC 변환이 끝났습니다.", "완료", MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
        }
        catch (Exception exc)
        {
            Log($"XKT 변환 실패: {exc.Message}");
            SetStatus("Failed");
        }
        finally
        {
            SetRunning(false);
        }
    }

    private void SelectPackSourceClicked(object? sender, EventArgs e)
    {
        using var dialog = new OpenFileDialog
        {
            Filter = _modeToggle.IsRevit ? "IFC files (*.ifc)|*.ifc|All files (*.*)|*.*" : "XML files (*.xml)|*.xml|All files (*.*)|*.*",
            Title = _modeToggle.IsRevit ? "Revit IFC 파일 선택" : "Advance Steel XML 파일 선택",
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            SetPackSource(dialog.FileName);
        }
    }

    private void BrowsePackOutputClicked(object? sender, EventArgs e)
    {
        using var dialog = new FolderBrowserDialog { Description = "팩 출력 폴더 선택", UseDescriptionForTitle = true };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            _packOutput.Text = dialog.SelectedPath;
        }
    }

    private void SetPackSource(string? path)
    {
        _packSource.Text = path ?? "";
        _packDropZone.FilePath = path;
        if (string.IsNullOrWhiteSpace(path)) return;
        _packName.Text = $"{Path.GetFileNameWithoutExtension(path)} 온톨로지팩";
        if (string.IsNullOrWhiteSpace(_packOutput.Text))
        {
            _packOutput.Text = Path.GetDirectoryName(path) ?? "";
        }
    }

    private void SyncPackMode()
    {
        _buildLocalCrab.Enabled = !_modeToggle.IsRevit;
        if (!_buildLocalCrab.Enabled) _buildLocalCrab.Checked = false;
        if (!string.IsNullOrWhiteSpace(_packSource.Text) && !IsValidPackSource(_packSource.Text))
        {
            SetPackSource(null);
            SetBottomStatus("팩 빌더 모드가 바뀌어 기존 원본 파일 선택을 초기화했습니다.");
        }
    }

    private bool IsValidPackSource(string path)
    {
        var extension = Path.GetExtension(path);
        return _modeToggle.IsRevit
            ? string.Equals(extension, ".ifc", StringComparison.OrdinalIgnoreCase)
            : string.Equals(extension, ".xml", StringComparison.OrdinalIgnoreCase);
    }

    private async Task BuildPackClicked()
    {
        if (_running) return;
        if (!File.Exists(_packSource.Text))
        {
            MessageBox.Show(this, "원본 IFC 또는 XML 파일을 선택하세요.", "입력 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        if (!IsValidPackSource(_packSource.Text))
        {
            MessageBox.Show(this, _modeToggle.IsRevit ? "Revit IFC 모드에서는 .ifc 파일을 선택하세요." : "Advance Steel XML 모드에서는 .xml 파일을 선택하세요.", "파일 형식 확인", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        if (string.IsNullOrWhiteSpace(_packName.Text))
        {
            MessageBox.Show(this, "팩 이름을 입력하세요.", "팩 이름 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        var output = string.IsNullOrWhiteSpace(_packOutput.Text)
            ? Path.GetDirectoryName(_packSource.Text) ?? Directory.GetCurrentDirectory()
            : Path.GetFullPath(_packOutput.Text);
        Directory.CreateDirectory(output);

        var options = new PackBuildOptions(
            _modeToggle.IsRevit ? "revit" : "advance",
            Path.GetFullPath(_packSource.Text),
            output,
            _packName.Text.Trim(),
            _packId.Text.Trim(),
            _includeReadme.Checked,
            _includeBackdata.Checked,
            _buildLocalCrab.Checked
        );

        var result = 1;
        try
        {
            SetRunning(true);
            _packPipeline.SetStep(0);
            _progress.Minimum = 0;
            _progress.Maximum = 4;
            _progress.Value = 1;
            _lastOutputFolder = output;
            Log("");
            Log($"팩 생성 시작: {options.PackName}");
            Log($"mode: {options.Mode}");
            Log($"source: {options.SourceFile}");

            var spec = PackEngine.BuildProcess(options);
            _packPipeline.SetStep(1);
            Log(spec.Description);
            result = await ProcessRunner.RunAsync(spec, message =>
            {
                RunOnUi(() =>
                {
                    Log(message);
                    if (_progress.Value < _progress.Maximum) _progress.Value += 1;
                    if (_progress.Value == 2) _packPipeline.SetStep(2);
                    if (_progress.Value >= 3) _packPipeline.SetStep(3);
                });
            });
            _progress.Value = _progress.Maximum;
            _packPipeline.SetStep(3);
        }
        catch (Exception exc)
        {
            Log($"팩 생성 실패: {exc.Message}");
            SetStatus("Failed");
        }
        finally
        {
            SetRunning(false);
        }

        if (result == 0)
        {
            SetStatus("Complete");
            AddHistory($"팩 생성 완료: {options.PackName} -> {options.OutputFolder}");
            MessageBox.Show(this, "온톨로지 팩 생성이 끝났습니다.", "완료", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        else
        {
            SetStatus("Failed");
            MessageBox.Show(this, "팩 생성 중 오류가 발생했습니다. 작업 로그를 확인하세요.", "실패", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    private void OpenOutputFolder()
    {
        var folder = _lastOutputFolder;
        if (string.IsNullOrWhiteSpace(folder) && !string.IsNullOrWhiteSpace(_packOutput.Text)) folder = _packOutput.Text;
        if (string.IsNullOrWhiteSpace(folder) && !string.IsNullOrWhiteSpace(_xktOutput.Text)) folder = _xktOutput.Text;
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            MessageBox.Show(this, "아직 열 수 있는 출력 폴더가 없습니다.", "출력 폴더 없음", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        Process.Start(new ProcessStartInfo("explorer.exe", folder) { UseShellExecute = true });
    }

    private static string HistoryPath()
    {
        var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "ModularOntologyBuilder");
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, "history.log");
    }

    private void AddHistory(string text)
    {
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss}  {text}";
        File.AppendAllText(HistoryPath(), line + Environment.NewLine, Encoding.UTF8);
        LoadHistory();
    }

    private void LoadHistory()
    {
        _historyList.Items.Clear();
        var path = HistoryPath();
        if (!File.Exists(path)) return;
        foreach (var line in File.ReadLines(path, Encoding.UTF8).Reverse().Take(200))
        {
            _historyList.Items.Add(line);
        }
    }

    private void OpenHistoryFile()
    {
        var path = HistoryPath();
        if (!File.Exists(path)) File.WriteAllText(path, "", Encoding.UTF8);
        Process.Start(new ProcessStartInfo("notepad.exe", path) { UseShellExecute = true });
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

    public static ProcessSpec ToProcess(ConverterSpec converter, string input, string output)
    {
        var args = new List<string>(converter.PrefixArgs) { "-s", input, "-f", "ifc", "-o", output };
        return new ProcessSpec(converter.FileName, args, Directory.GetCurrentDirectory(), converter.Description);
    }

    private static string OutputFor(string inputFile, string? outputFolder)
    {
        return string.IsNullOrWhiteSpace(outputFolder)
            ? Path.ChangeExtension(inputFile, ".xkt")
            : Path.Combine(outputFolder, Path.GetFileNameWithoutExtension(inputFile) + ".xkt");
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

internal static class PackEngine
{
    public static ProcessSpec BuildProcess(PackBuildOptions options)
    {
        var scriptDir = ResolveScriptDir();
        var script = options.Mode == "revit"
            ? Path.Combine(scriptDir, "build_revit_ifc_workset_pack_cli.py")
            : Path.Combine(scriptDir, "build_advance_steel_pack_cli.py");
        if (!File.Exists(script)) throw new FileNotFoundException("Pack builder script not found.", script);

        var args = new List<string>
        {
            script,
            options.Mode == "revit" ? "--ifc" : "--xml",
            options.SourceFile,
            "--out",
            options.OutputFolder,
            "--pack-name",
            options.PackName,
        };
        if (!string.IsNullOrWhiteSpace(options.PackId))
        {
            args.Add("--pack-id");
            args.Add(options.PackId);
        }
        if (options.IncludeReadme) args.Add("--include-readme");
        if (options.IncludeBackdata) args.Add("--include-backdata");
        if (options.Mode == "advance" && options.BuildLocalCrab) args.Add("--localcrab");

        var python = ResolvePython();
        return new ProcessSpec(python, args, options.OutputFolder, $"python: {python}{Environment.NewLine}script: {script}");
    }

    private static string ResolvePython()
    {
        var explicitPython = Environment.GetEnvironmentVariable("MODULAR_ONTOLOGY_PYTHON");
        return !string.IsNullOrWhiteSpace(explicitPython) && File.Exists(explicitPython) ? explicitPython : "python";
    }

    private static string ResolveScriptDir()
    {
        var candidates = new List<string>
        {
            Path.Combine(AppContext.BaseDirectory, "scripts"),
            Path.Combine(Directory.GetCurrentDirectory(), "tools", "ontology-builder", "scripts"),
            Path.Combine(Directory.GetCurrentDirectory(), "scripts"),
        };
        AddParentCandidates(candidates, AppContext.BaseDirectory);
        AddParentCandidates(candidates, Directory.GetCurrentDirectory());

        foreach (var candidate in candidates.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (File.Exists(Path.Combine(candidate, "build_revit_ifc_workset_pack_cli.py")) &&
                File.Exists(Path.Combine(candidate, "build_advance_steel_pack_cli.py")))
            {
                return candidate;
            }
        }
        return candidates[0];
    }

    private static void AddParentCandidates(List<string> candidates, string path)
    {
        var current = new DirectoryInfo(path);
        while (current is not null)
        {
            candidates.Add(Path.Combine(current.FullName, "tools", "ontology-builder", "scripts"));
            current = current.Parent;
        }
    }
}

internal static class ProcessRunner
{
    public static async Task<int> RunAsync(ProcessSpec spec, Action<string> log)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = spec.FileName,
            WorkingDirectory = spec.WorkingDirectory,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var arg in spec.Arguments) startInfo.ArgumentList.Add(arg);

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
            log($"실행 실패: {exc.Message}");
            return 1;
        }
    }
}
