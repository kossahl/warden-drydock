export default async function globalTeardown() {
  await fetch("http://127.0.0.1:4173/__test_shutdown__", { method: "POST" });
}
