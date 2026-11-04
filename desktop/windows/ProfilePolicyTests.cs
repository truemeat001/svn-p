using System;
using System.IO;
using System.Runtime.InteropServices;
namespace SVNDeskDesktop {
    internal static class ProfilePolicyTests {
        private static int checks;
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
        [return: MarshalAs(UnmanagedType.I1)]
        private static extern bool CreateSymbolicLinkW(string link, string target, uint flags);
        private static void Check(bool pass) { ++checks; if (!pass) throw new Exception("Profile check " + checks + " failed"); }
        private static void Reject(Action action) { bool rejected=false; try { action(); } catch { rejected=true; } Check(rejected); }
        internal static int Main(string[] args) {
            string fixture=null; string link=null; bool fixtureOwned=false; bool linkOwned=false;
            try {
                if (Environment.OSVersion.Platform != PlatformID.Win32NT || args.Length != 1) throw new ArgumentException("Pass one existing isolated Windows test parent.");
                fixture = Path.Combine(Path.GetFullPath(args[0]), "profile-policy-" + Guid.NewGuid().ToString("N"));
                using (var seed = ProfilePolicy.Create(fixture)) { fixtureOwned=true; Check(Directory.Exists(seed.ProfileDirectory)); }
                string fresh=Path.Combine(fixture,"fresh");
                using (var profile = ProfilePolicy.Create(fresh)) {
                    Check(Directory.Exists(fresh)); Check(Directory.Exists(profile.ProfileDirectory));
                    Check(Path.GetDirectoryName(profile.ProfileDirectory) == fresh); profile.Recheck();
                    Reject(delegate { ProfilePolicy.Create(fresh); });
                    Reject(delegate { Directory.Move(fresh, fresh + "-moved"); });
                }
                string file=Path.Combine(fixture,"existing-file"); File.WriteAllText(file,"fixture");
                Reject(delegate { ProfilePolicy.Create(file); });
                Reject(delegate { ProfilePolicy.Create(Path.Combine(fixture,"missing-parent","fresh")); });
                foreach (string marker in new[] { ".git", ".svn" }) {
                    string parent=Path.Combine(fixture,marker.Substring(1)+"-working-copy"); Directory.CreateDirectory(parent);
                    if (marker == ".git") File.WriteAllText(Path.Combine(parent,marker),"gitdir: fixture"); else Directory.CreateDirectory(Path.Combine(parent,marker));
                    string child=Path.Combine(parent,"fresh"); Reject(delegate { ProfilePolicy.Create(child); }); Check(!Directory.Exists(child));
                }
                string changing=Path.Combine(fixture,"changing");
                using (var profile=ProfilePolicy.Create(changing)) {
                    string marker=Path.Combine(changing,".git"); File.WriteAllText(marker,"fixture");
                    Reject(delegate { profile.Recheck(); }); File.Delete(marker);
                }
                string target=Path.Combine(fixture,"link-target"); Directory.CreateDirectory(target);
                link=Path.Combine(fixture,"reparse-parent");
                if (!CreateSymbolicLinkW(link,target,1|2)) {
                    int error=Marshal.GetLastWin32Error();
                    if (error != 1314) throw new System.ComponentModel.Win32Exception(error,"Symlink fixture creation failed (only privilege 1314 can skip).");
                    Console.WriteLine("SKIP symlink ancestor case: Windows error 1314 (required privilege unavailable)");
                    link=null;
                } else {
                    linkOwned=true;
                    Reject(delegate { ProfilePolicy.Create(Path.Combine(link,"fresh")); });
                    Check(!Directory.Exists(Path.Combine(target,"fresh"))); Directory.Delete(link); link=null; linkOwned=false;
                }
                Console.WriteLine("PASS: " + checks + " Windows profile-policy checks"); return 0;
            } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
            finally {
                if (linkOwned && link != null && Directory.Exists(link)) Directory.Delete(link);
                if (fixtureOwned && fixture != null && Directory.Exists(fixture)) Directory.Delete(fixture,true);
            }
        }
    }
}
