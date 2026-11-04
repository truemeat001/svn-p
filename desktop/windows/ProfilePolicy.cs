using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace SVNDeskDesktop {
    // Only native CLI uses this policy. No HTTP or JavaScript surface exists.
    internal sealed class ProfilePolicy : IDisposable {
        private readonly List<SafeFileHandle> pins = new List<SafeFileHandle>();
        internal string Root { get; private set; }
        internal string ProfileDirectory { get; private set; }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern bool CreateDirectoryW(string path, IntPtr security);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern uint GetFileAttributesW(string path);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);

        internal static string Syntax(string path) {
            if (String.IsNullOrWhiteSpace(path) || path.Length <= 3 || path.Length >= 240 || !((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')) || path[1] != ':' || path[2] != '\\' || path.IndexOf('/') >= 0 || path.EndsWith("\\",StringComparison.Ordinal)) throw new ArgumentException("Profile root must be a new absolute local drive directory.");
            string[] segments = path.Substring(3).Split('\\');
            foreach (string segment in segments) {
                if (String.Equals(segment,".git",StringComparison.OrdinalIgnoreCase) || String.Equals(segment,".svn",StringComparison.OrdinalIgnoreCase) || segment.Length == 0 || segment == "." || segment == ".." || segment.EndsWith(".",StringComparison.Ordinal) || segment.EndsWith(" ",StringComparison.Ordinal) || segment.IndexOfAny(new[] { ':','*','?','"','<','>','|' }) >= 0) throw new ArgumentException("Unsafe profile path component.");
                foreach (char c in segment) if (c < 32) throw new ArgumentException("Control character in profile path.");
                string stem = segment.Split('.')[0].ToUpperInvariant();
                if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL" || stem == "CONIN$" || stem == "CONOUT$" || (stem.Length == 4 && (stem.StartsWith("COM",StringComparison.Ordinal) || stem.StartsWith("LPT",StringComparison.Ordinal)) && ("123456789\u00b9\u00b2\u00b3".IndexOf(stem[3]) >= 0))) throw new ArgumentException("Device names are not profile directories.");
            }
            return path;
        }
        private static uint Attributes(string path, bool mayBeAbsent) {
            uint attributes = GetFileAttributesW(path);
            if (attributes != UInt32.MaxValue) return attributes;
            int error = Marshal.GetLastWin32Error();
            if (mayBeAbsent && (error == 2 || error == 3)) return UInt32.MaxValue;
            throw new Win32Exception(error, "Profile path could not be inspected.");
        }
        private static void DirectorySafe(string path) {
            uint attributes = Attributes(path,false);
            if ((attributes & 0x10) == 0 || (attributes & 0x400) != 0) throw new IOException("Profile ancestor must be a regular non-reparse directory.");
            foreach (string marker in new[] { ".svn", ".git" }) {
                if (Attributes(Path.Combine(path,marker),true) != UInt32.MaxValue) throw new IOException("Profile directory cannot be inside a detected working copy.");
            }
        }
        private static List<string> Ancestors(string path) {
            var chain = new List<string>();
            for (DirectoryInfo directory = new DirectoryInfo(path); directory != null; directory = directory.Parent) chain.Add(directory.FullName);
            chain.Reverse(); return chain;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        private static extern uint QueryDosDeviceW(string device, System.Text.StringBuilder target, int size);
        internal static void ValidateFresh(string root) {
            Syntax(root);
            var target = new System.Text.StringBuilder(1024);
            if (QueryDosDeviceW(root.Substring(0,2),target,target.Capacity) == 0) throw new Win32Exception(Marshal.GetLastWin32Error(),"Could not inspect local drive.");
            if (target.ToString().StartsWith(@"\??\",StringComparison.Ordinal)) throw new IOException("Substituted drive aliases are not allowed for profile roots.");
            DriveType drive = new DriveInfo(root.Substring(0,3)).DriveType;
            if (drive != DriveType.Fixed && drive != DriveType.Removable && drive != DriveType.Ram) throw new IOException("Profile must be on a local writable drive.");
            foreach (string parent in Ancestors(Path.GetDirectoryName(root))) DirectorySafe(parent);
            if (Attributes(root,true) != UInt32.MaxValue) throw new IOException("Profile root must not already exist.");
        }
        private void Pin(string path) {
            // Deny delete/rename while the host owns the directory; open the entry itself.
            SafeFileHandle handle = CreateFileW(path, 0, 1 | 2, IntPtr.Zero, 3, 0x02000000 | 0x00200000, IntPtr.Zero);
            if (handle.IsInvalid) { int error = Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(error,"Could not pin profile directory."); }
            pins.Add(handle); DirectorySafe(path);
        }
        private static void CreateFresh(string path) {
            // Unlike Directory.CreateDirectory, ERROR_ALREADY_EXISTS is always a failure.
            if (!CreateDirectoryW(path,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error(),"Profile directory was not created fresh.");
        }
        internal static ProfilePolicy Create(string root) {
            ValidateFresh(root);
            var policy = new ProfilePolicy { Root = root };
            try {
                foreach (string parent in Ancestors(Path.GetDirectoryName(root))) policy.Pin(parent);
                ValidateFresh(root);
                CreateFresh(root); policy.Pin(root);
                policy.ProfileDirectory = Path.Combine(root, Guid.NewGuid().ToString("N"));
                CreateFresh(policy.ProfileDirectory); policy.Pin(policy.ProfileDirectory);
                policy.Recheck(); return policy;
            } catch { policy.Dispose(); throw; }
        }
        internal void Recheck() {
            foreach (string directory in Ancestors(ProfileDirectory)) DirectorySafe(directory);
        }
        public void Dispose() { foreach (SafeFileHandle pin in pins) pin.Dispose(); pins.Clear(); }
    }
}
