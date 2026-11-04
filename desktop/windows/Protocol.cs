using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace SVNDeskDesktop {
    internal static class Protocol {
        internal const int MaximumHandshakeBytes = 4096;
        internal static string[] NativeArguments(string[] input, out string profileRoot) {
            profileRoot = null; var remaining = new List<string>();
            for (int i = 0; i < input.Length; ++i) {
                if (input[i] != "--profile-root") { remaining.Add(input[i]); continue; }
                if (profileRoot != null || ++i == input.Length) throw new ArgumentException("Duplicate or missing profile root.");
                profileRoot = ProfilePolicy.Syntax(input[i]);
            }
            return remaining.ToArray();
        }
        internal static string[] Arguments(string[] input) {
            var output = new List<string>(); bool svnSeen = false;
            for (int i = 0; i < input.Length; ++i) {
                string option = input[i];
                switch (option) {
                    case "--workspace": case "--scan-parent": case "--svn":
                        if (option == "--svn" && svnSeen) throw new ArgumentException("Duplicate SVN option");
                        if (option == "--svn") svnSeen = true;
                        if (++i == input.Length || String.IsNullOrWhiteSpace(input[i]) || input[i].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException("Missing path");
                        output.Add(option); output.Add(Path.GetFullPath(input[i])); break;
                    case "--enable-changelists": case "--enable-shelves": case "--enable-commits": case "--enable-locks": case "--enable-adds": case "--enable-file-ops":
                        output.Add(option); break;
                    default: throw new ArgumentException("Unsupported option");
                }
            }
            return output.ToArray();
        }
        // CommandLineToArgvW quoting: no cmd.exe, no shell interpretation.
        internal static string Quote(string value) {
            var result = new StringBuilder("\""); int slashes = 0;
            foreach (char c in value) {
                if (c == '\\') { ++slashes; continue; }
                result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
                result.Append(c); slashes = 0;
            }
            result.Append('\\', slashes * 2); return result.Append('"').ToString();
        }
        internal static string CommandLine(string[] arguments) {
            var result = new List<string>(); foreach (string argument in arguments) result.Add(Quote(argument));
            return String.Join(" ", result);
        }
        internal static async Task<byte[]> ReadHandshake(Stream stream) {
            var bytes = new List<byte>(); byte[] next = new byte[1];
            for (int i = 0; i < MaximumHandshakeBytes; ++i) {
                if (await stream.ReadAsync(next, 0, 1).ConfigureAwait(false) == 0) throw new InvalidDataException("Readiness EOF");
                if (next[0] == 10) return bytes.ToArray(); bytes.Add(next[0]);
            }
            throw new InvalidDataException("Readiness exceeds bound");
        }
        // Await completion (including failure) before taking exclusive stdout ownership.
        // This is attached immediately, so a UI timeout cannot abandon a noisy tail.
        internal static async Task DrainAfterHandshake(Task<byte[]> handshake, Stream stream) {
            try { await handshake.ConfigureAwait(false); } catch { /* Observe readiness failure. */ }
            try {
                byte[] buffer = new byte[4096];
                while (await stream.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false) != 0) {}
            } catch (IOException) {} catch (ObjectDisposedException) {}
        }
        internal static Uri ReadyOrigin(byte[] bytes, int expectedPid) {
            var utf8 = new UTF8Encoding(false, true);
            var serializer = new JavaScriptSerializer { MaxJsonLength = MaximumHandshakeBytes, RecursionLimit = 4 };
            var data = serializer.DeserializeObject(utf8.GetString(bytes)) as Dictionary<string, object>;
            if (data == null || data.Count != 3 || !data.ContainsKey("protocol") || !data.ContainsKey("pid") || !data.ContainsKey("origin") || !(data["pid"] is int) || (int)data["pid"] != expectedPid || !Object.Equals(data["protocol"], "svn-desk-desktop/1")) throw new InvalidDataException("Wrong bridge peer");
            string value = data["origin"] as string; Uri uri;
            if (!Uri.TryCreate(value, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.Port <= 0 || uri.Port == 80 || uri.AbsolutePath != "/" || uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0 || value != "http://127.0.0.1:" + uri.Port.ToString(System.Globalization.CultureInfo.InvariantCulture)) throw new InvalidDataException("Noncanonical loopback origin");
            return uri;
        }
        internal static bool SameOrigin(string value, Uri origin) {
            Uri candidate;
            return origin != null && Uri.TryCreate(value, UriKind.Absolute, out candidate) && candidate.Scheme == origin.Scheme && candidate.Host == origin.Host && candidate.Port == origin.Port && candidate.UserInfo.Length == 0;
        }
    }
}
