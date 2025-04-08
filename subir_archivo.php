<?php
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $archivo = $_FILES['archivo'];
    $permitidos = ['pdf', 'doc', 'docx', 'png', 'jpg', 'jpeg'];

    $extension = strtolower(pathinfo($archivo['name'], PATHINFO_EXTENSION));

    if (!in_array($extension, $permitidos)) {
        exit('Formato no permitido.');
    }

    $ruta_destino = __DIR__ . '/uploads/' . basename($archivo['name']);
    move_uploaded_file($archivo['tmp_name'], $ruta_destino);

    echo "Archivo subido: " . htmlspecialchars($archivo['name']);
}
?>
