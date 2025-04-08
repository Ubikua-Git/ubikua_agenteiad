<?php
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $mensaje_usuario = $_POST['mensaje'];
    $datos = json_encode(['mensaje' => $mensaje_usuario]);

    $ch = curl_init('https://api-asistente-rghp.onrender.com/consulta');
    curl_setopt($ch, CURLOPT_POSTFIELDS, $datos);
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);

    $respuesta = curl_exec($ch);
    curl_close($ch);

    $respuesta_json = json_decode($respuesta, true);
    echo nl2br(htmlspecialchars($respuesta_json['respuesta']));
}
?>
