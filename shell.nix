# Template
with import <nixpkgs> { };

mkShell {

  nativeBuildInputs = [
    direnv
    python312Packages.psycopg2
    python312Packages.aiohttp
    poetry
    postgresql_16_jit
  ];

  NIX_ENFORCE_PURITY = true;

  shellHook = ''
  '';
}
