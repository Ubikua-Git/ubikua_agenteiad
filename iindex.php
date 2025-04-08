<!DOCTYPE html>
<html lang="es">
<head>
  <title>Agente Ashotel IA</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 p-8 flex justify-center">
  <div class="w-full max-w-lg">
    <h1 class="text-2xl font-bold mb-4">Consulta a tu asistente Ashotel</h1>
    <textarea id="consulta" class="border p-4 w-full h-32 rounded-lg" placeholder="Escribe tu consulta aquí..."></textarea>
    <button onclick="enviarConsulta()" class="mt-4 bg-blue-600 text-white py-2 px-4 rounded">Enviar</button>
    <div id="respuesta" class="mt-4 p-4 bg-white rounded-lg shadow"></div>
  </div>

<script>
  function enviarConsulta() {
    let mensaje = document.getElementById('consulta').value;
    fetch('consulta.php', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'mensaje=' + encodeURIComponent(mensaje)
    })
    .then(res => res.text())
    .then(respuesta => {
      document.getElementById('respuesta').innerHTML = respuesta;
    });
  }
</script>
</body>
</html>
