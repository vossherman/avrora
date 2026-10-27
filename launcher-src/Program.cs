using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Aurora unofficial client launcher")]
[assembly: AssemblyVersion("0.1.0.0")]

internal static class Program
{
    [STAThread]
    private static void Main(string[] arguments)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "pythonw.exe");
        string entry = Path.Combine(root, "bootstrap.pyw");
#if MOCK
        string mode = " --mock";
#elif CHECK
        string mode = " --check";
        if (Array.IndexOf(arguments, "--quiet") >= 0) mode += " --quiet";
#else
        string mode = "";
#endif
        try
        {
            if (!File.Exists(python) || !File.Exists(entry))
                throw new FileNotFoundException("Не найдены файлы клиента. Скопируйте папку релиза целиком.");
            ProcessStartInfo info = new ProcessStartInfo(python, "-B \"" + entry + "\"" + mode);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WindowStyle = ProcessWindowStyle.Hidden;
#if CHECK
            using (Process child = Process.Start(info))
            {
                child.WaitForExit();
                Environment.ExitCode = child.ExitCode;
            }
#else
            Process.Start(info);
#endif
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Аврора v0.1 — ошибка запуска", MessageBoxButtons.OK,
                            MessageBoxIcon.Error);
        }
    }
}
