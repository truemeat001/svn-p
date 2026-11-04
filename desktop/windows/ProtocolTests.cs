using System;
using System.IO;
using System.Text;
namespace SVNDeskDesktop {
    internal static class ProtocolTests {
        private static int checks;
        private static void Check(bool value) { ++checks; if (!value) throw new Exception("Check " + checks + " failed"); }
        private static void Reject(Action action) { bool rejected = false; try { action(); } catch { rejected = true; } Check(rejected); }
        private static byte[] Ready(string origin, int pid) { return Encoding.UTF8.GetBytes("{\"protocol\":\"svn-desk-desktop/1\",\"origin\":\"" + origin + "\",\"pid\":" + pid + "}"); }
        public static int Main() {
            try {
                Uri own = Protocol.ReadyOrigin(Ready("http://127.0.0.1:49152", 7), 7);
                Check(Protocol.SameOrigin("http://127.0.0.1:49152/path", own));
                foreach (string bad in new[] { "http://localhost:49152", "http://127.1:49152", "https://127.0.0.1:49152", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:49152/", "http://127.0.0.1:49152/path", "http://user@127.0.0.1:49152", "http://127.0.0.1:49152?x" }) Reject(delegate { Protocol.ReadyOrigin(Ready(bad,7),7); });
                Reject(delegate { Protocol.ReadyOrigin(Ready("http://127.0.0.1:49152",8),7); });
                Reject(delegate { Protocol.ReadyOrigin(Encoding.UTF8.GetBytes("diagnostic text"),7); });
                foreach (string bad in new[] { "http://127.0.0.1:49153", "https://example.org", "file:///C:/a", "http://user@127.0.0.1:49152" }) Check(!Protocol.SameOrigin(bad,own));
                Check(Encoding.UTF8.GetString(Protocol.ReadHandshake(new MemoryStream(Encoding.UTF8.GetBytes("ok\nextra"))).GetAwaiter().GetResult()) == "ok");
                Reject(delegate { Protocol.ReadHandshake(new MemoryStream(new byte[4097])).GetAwaiter().GetResult(); });
                Reject(delegate { Protocol.ReadHandshake(new MemoryStream(new byte[0])).GetAwaiter().GetResult(); });
                // More than normal pipe capacity: failed readiness must not strand tail bytes.
                var noisy = new MemoryStream(new byte[256 * 1024]);
                var failedReadiness = Protocol.ReadHandshake(noisy);
                Protocol.DrainAfterHandshake(failedReadiness, noisy).GetAwaiter().GetResult();
                Check(failedReadiness.IsFaulted); Check(noisy.Position == noisy.Length);
                var tailed = new MemoryStream(Encoding.UTF8.GetBytes("invalid-json\n" + new string('x', 256 * 1024)));
                var invalidReadiness = Protocol.ReadHandshake(tailed);
                var tailDrain = Protocol.DrainAfterHandshake(invalidReadiness, tailed);
                Reject(delegate { Protocol.ReadyOrigin(invalidReadiness.GetAwaiter().GetResult(), 7); });
                tailDrain.GetAwaiter().GetResult(); Check(tailed.Position == tailed.Length);
                var delayed = new System.Threading.Tasks.TaskCompletionSource<byte[]>();
                var lateTail = new MemoryStream(new byte[256 * 1024]);
                var delayedDrain = Protocol.DrainAfterHandshake(delayed.Task, lateTail);
                Check(lateTail.Position == 0 && !delayedDrain.IsCompleted);
                delayed.SetResult(Encoding.UTF8.GetBytes("late readiness"));
                delayedDrain.GetAwaiter().GetResult(); Check(lateTail.Position == lateTail.Length);
                Check(Protocol.Arguments(new string[0]).Length == 0);
                Check(Protocol.Arguments(new[] { "--workspace", "space name", "--workspace", "other", "--enable-commits" }).Length == 5);
                Reject(delegate { Protocol.Arguments(new[] { "--backend-source", "evil" }); });
                Reject(delegate { Protocol.Arguments(new[] { "--workspace", "--enable-commits" }); });
                Reject(delegate { Protocol.Arguments(new[] { "--svn", "a", "--svn", "b" }); });
                Check(Protocol.Quote("C:\\space name\\") == "\"C:\\space name\\\\\"");
                Check(Protocol.Quote("x\"y") == "\"x\\\"y\"");
                Check(Protocol.Quote("") == "\"\"");
                Console.WriteLine("PASS: " + checks + " desktop protocol checks"); return 0;
            } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
    }
}
