// Self-contained Windows launcher. Only Windows/.NET framework code runs before
// elevation; the embedded Python setup runs from a protected staging directory.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Windows.Forms;

static class SetupBootstrap
{
    const string SetupName = "SLS_Mass_Notify_Installer.exe";
    static readonly HashSet<string> Writers = new HashSet<string> {
        "S-1-5-18", "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    };

    static bool Elevated()
    {
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
    }

    static string Quote(string value)
    {
        // Windows CommandLineToArgvW quoting, including trailing backslashes.
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            result.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
            result.Append(ch);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    static void NoReparse(string path)
    {
        for (string current = Path.GetFullPath(path); current != null; current = Path.GetDirectoryName(current)) {
            if ((Directory.Exists(current) || File.Exists(current)) &&
                (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Setup refuses a redirected path: " + current);
        }
    }

    static void ProtectedDirectory(string path)
    {
        NoReparse(path);
        DirectorySecurity security = Directory.GetAccessControl(path);
        if (new RawSecurityDescriptor(security.GetSecurityDescriptorBinaryForm(), 0).DiscretionaryAcl == null)
            throw new UnauthorizedAccessException("Setup staging folder has unrestricted access: " + path);
        if (!Writers.Contains(security.GetOwner(typeof(SecurityIdentifier)).Value))
            throw new UnauthorizedAccessException("Setup staging folder has an untrusted owner: " + path);
        foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
            if (rule.AccessControlType != AccessControlType.Allow ||
                (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0) continue;
            if (((int)rule.FileSystemRights & 0x500D0156) != 0 && !Writers.Contains(rule.IdentityReference.Value))
                throw new UnauthorizedAccessException("Setup staging folder permits non-administrator writes: " + path);
        }
    }

    static void CreateProtected(string path)
    {
        if (Directory.Exists(path)) { ProtectedDirectory(path); return; }
        ProtectedDirectory(Path.GetDirectoryName(path));
        DirectorySecurity security = new DirectorySecurity();
        security.SetSecurityDescriptorSddlForm("O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)");
        Directory.CreateDirectory(path, security);
        ProtectedDirectory(path);
    }

    static MemoryStream Payload()
    {
        MemoryStream bytes = new MemoryStream();
        using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("setup-payload.zip")) {
            if (resource == null) throw new InvalidDataException("The installer payload is missing.");
            resource.CopyTo(bytes);
        }
        bytes.Position = 0;
        string hash;
        using (SHA256 sha = SHA256.Create())
            hash = BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        if (hash != BuildInfo.PayloadSha256) throw new InvalidDataException("The installer payload is damaged. Download it again.");
        bytes.Position = 0;
        return bytes;
    }

    static void Unpack(Stream stream, string destination)
    {
        string root = destination == null ? null : Path.GetFullPath(destination) + Path.DirectorySeparatorChar;
        HashSet<string> names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long total = 0;
        using (ZipArchive archive = new ZipArchive(stream, ZipArchiveMode.Read, true)) {
            if (archive.Entries.Count > 20000) throw new InvalidDataException("Too many setup files.");
            foreach (ZipArchiveEntry entry in archive.Entries) {
                string name = entry.FullName;
                if (name.Length == 0 || name.Contains("\\") || name.Contains(":") || name.StartsWith("/") || !names.Add(name))
                    throw new InvalidDataException("Invalid setup payload filename.");
                foreach (string part in name.Split('/'))
                    if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(" ") || part.EndsWith("."))
                        throw new InvalidDataException("Invalid setup payload path.");
                total = checked(total + entry.Length);
                if (entry.Length > 256 * 1024 * 1024 || total > 512 * 1024 * 1024)
                    throw new InvalidDataException("Setup payload exceeds its size limit.");
                if (root != null) {
                    string target = Path.GetFullPath(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
                    if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Setup payload escapes its staging directory.");
                    NoReparse(target);
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    using (Stream source = entry.Open())
                    using (FileStream output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        source.CopyTo(output);
                }
            }
            if (!names.Contains(SetupName) || !names.Contains("_internal/python314.dll"))
                throw new InvalidDataException("The installer runtime is incomplete.");
        }
    }

    static void Cleanup(string stage)
    {
        // Only remove our randomly named, protected staging folder. Check all
        // descendants before recursive removal so redirected paths are rejected.
        ProtectedDirectory(stage);
        foreach (string entry in Directory.EnumerateFileSystemEntries(stage)) {
            NoReparse(entry);
            if (Directory.Exists(entry)) Cleanup(entry);
            else File.Delete(entry);
        }
        Directory.Delete(stage, false);
    }

    [STAThread]
    static int Main(string[] args)
    {
        bool quiet = Array.IndexOf(args, "--silent") >= 0 || Array.IndexOf(args, "--quiet") >= 0 || Array.IndexOf(args, "--check-package") >= 0;
        try {
            // Build verification is deliberately non-elevating and never installs.
            if (args.Length == 1 && args[0] == "--check-package") {
                using (MemoryStream payload = Payload()) Unpack(payload, null);
                Console.WriteLine("SLS_PACKAGE_OK " + BuildInfo.Version + " " + BuildInfo.PayloadSha256 + " " + BuildInfo.SourceSha256);
                return 0;
            }
            if (!Elevated()) {
                if (quiet) return 740;
                using (Process elevated = Process.Start(new ProcessStartInfo {
                    FileName = Assembly.GetExecutingAssembly().Location,
                    Arguments = String.Join(" ", Array.ConvertAll(args, Quote)),
                    WorkingDirectory = Environment.SystemDirectory,
                    UseShellExecute = true, Verb = "runas"
                })) {
                    if (elevated == null) throw new IOException("Windows could not start administrator setup.");
                    elevated.WaitForExit();
                    return elevated.ExitCode;
                }
            }
            string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
            ProtectedDirectory(programFiles);
            string parent = Path.Combine(programFiles, "Southland Servers Group");
            CreateProtected(parent);
            string stage = Path.Combine(parent, ".sls-setup-" + Guid.NewGuid().ToString("N"));
            CreateProtected(stage);
            try {
                using (MemoryStream payload = Payload()) Unpack(payload, stage);
                using (Process setup = Process.Start(new ProcessStartInfo {
                    FileName = Path.Combine(stage, SetupName),
                    Arguments = String.Join(" ", Array.ConvertAll(args, Quote)),
                    WorkingDirectory = stage, UseShellExecute = false
                })) {
                    setup.WaitForExit();
                    return setup.ExitCode;
                }
            }
            finally {
                try { Cleanup(stage); }
                catch (Exception cleanup) {
                    if (!quiet) MessageBox.Show("Setup staging files could not be removed:\n" + stage + "\n\n" + cleanup.Message,
                                                "SLS Mass Notify Setup", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
        }
        catch (Win32Exception error) {
            if (error.NativeErrorCode == 1223) return 740; // UAC cancelled.
            if (!quiet) MessageBox.Show(error.Message, "SLS Mass Notify Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1603;
        }
        catch (Exception error) {
            if (quiet) Console.Error.WriteLine(error.Message);
            if (!quiet) MessageBox.Show(error.Message, "SLS Mass Notify Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1603;
        }
    }
}
