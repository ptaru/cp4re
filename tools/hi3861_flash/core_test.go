package main
import ("testing";"os")
func TestRejectTruncated(t *testing.T) {
 for _,n:=range []int{0,1,30,552607,552609} {
  if validateOTA(make([]byte,n))==nil {t.Fatalf("accepted invalid size %d",n)}
 }
}
func TestWorkerEncoding(t *testing.T) {if len(workerBytes())!=52{t.Fatal("worker length")}}
func TestCandidateAndMutations(t *testing.T) {
 path:=os.Getenv("CP4_TEST_IMAGE");if path==""{t.Skip("set CP4_TEST_IMAGE to an offline-built OTA")}
 b,e:=os.ReadFile(path);if e!=nil{t.Fatal(e)};if e=validateOTA(b);e!=nil{t.Fatal(e)}
 for _,off:=range []int{0,8,0x18,0x180,0x2b0,0x2c0,0x3c0,len(b)-1} {
  c:=append([]byte{},b...);c[off]^=1;if validateOTA(c)==nil{t.Fatalf("accepted mutation at %#x",off)}
 }
}
