const pages = ['home', 'metodologia', 'sobre', 'visualizacoes'];
const sections = new Map(pages.map(id => [id, document.getElementById(id)]));
const navLinks = [...document.querySelectorAll('[data-route]')];
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let currentRoute = null;
let transitionTicket = 0;

function activateRoute(route) {
  const id = pages.includes(route) ? route : 'home';
  if (id === currentRoute) return;
  const previous = currentRoute && sections.get(currentRoute);
  const next = sections.get(id);
  const ticket = ++transitionTicket;
  // Exibe a nova seção imediatamente, para que controles nunca fiquem inacessíveis.
  // A antiga permanece visível só durante o fade curto e é escondida ao terminar.
  sections.forEach((section, key) => {
    section.classList.remove('page-enter', 'page-exit');
    if (key !== id && key !== currentRoute) section.hidden = true;
  });
  next.hidden = false;
  if (previous && previous !== next) {
    if (!reducedMotion.matches) {
      previous.hidden = false;
      previous.classList.add('page-exit');
      next.classList.add('page-enter');
      window.setTimeout(() => {
        if (ticket !== transitionTicket) return;
        previous.hidden = true;
        previous.classList.remove('page-exit');
        next.classList.remove('page-enter');
      }, 340);
    } else {
      previous.hidden = true;
    }
  }
  currentRoute = id;
  navLinks.forEach(link => {
    const active = link.dataset.route === id;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const names = {home:'Home',metodologia:'Metodologia',sobre:'Sobre',visualizacoes:'Visualizações'};
  document.title = `${names[id]} — #eleições2026`;
  window.scrollTo(0, 0);
}
function onRouteChange() { activateRoute(location.hash.slice(1).toLowerCase()); }
window.addEventListener('hashchange', onRouteChange);
onRouteChange();

// Slides visuais da Home — separados das rotas principais.
const homeSlides = [...document.querySelectorAll('[data-home-slide]')];
const homeControls = document.getElementById('homeSlideControls');
const previousSlideButton = document.getElementById('homePrevious');
const nextSlideButton = document.getElementById('homeNext');
const slideDots = [...document.querySelectorAll('[data-go-slide]')];
let currentHomeSlide = 0;

function showHomeSlide(index) {
  currentHomeSlide = Math.max(0, Math.min(homeSlides.length - 1, index));
  homeSlides.forEach((slide, i) => {
    const active = i === currentHomeSlide;
    slide.hidden = !active;
    slide.classList.remove('home-slide-enter');
    if (active && i !== 0 && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      void slide.offsetWidth; // reinicia a transição ao navegar
      slide.classList.add('home-slide-enter');
    }
  });
  homeControls.hidden = currentHomeSlide === 0;
  previousSlideButton.disabled = currentHomeSlide === 0;
  nextSlideButton.disabled = currentHomeSlide === homeSlides.length - 1;
  slideDots.forEach((dot,i) => {
    if (i === currentHomeSlide) dot.setAttribute('aria-current','step');
    else dot.removeAttribute('aria-current');
  });
}
document.getElementById('startHomeSlides').addEventListener('click', () => showHomeSlide(1));
previousSlideButton.addEventListener('click', () => showHomeSlide(currentHomeSlide - 1));
nextSlideButton.addEventListener('click', () => showHomeSlide(currentHomeSlide + 1));
slideDots.forEach(dot => dot.addEventListener('click', () => showHomeSlide(Number(dot.dataset.goSlide))));
document.addEventListener('keydown', event => {
  if (document.getElementById('home').hidden || event.altKey || event.ctrlKey || event.metaKey) return;
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) return;
  if (event.key === 'ArrowRight' && currentHomeSlide > 0) { event.preventDefault();showHomeSlide(currentHomeSlide+1); }
  if (event.key === 'ArrowLeft' && currentHomeSlide > 0) { event.preventDefault();showHomeSlide(currentHomeSlide-1); }
});
// O retorno explícito à Home pela navbar reinicia a sequência.
document.querySelectorAll('a[href="#home"]').forEach(link => link.addEventListener('click', () => showHomeSlide(0)));
showHomeSlide(0);


// Alinha as pontas das formas amarelas às laterais do viewport.
// Mantém proporções nativas dos SVGs sem deformá-los.
const slideArtwork = [
  { selector: '.slide-one .home-slide-art', viewWidth: 2069, viewHeight: 1242, tip: 1836, side: 'right', offsetX: 0.25 },
  { selector: '.slide-two .home-slide-art', viewWidth: 2097, viewHeight: 1242, tip: 261, side: 'left', offsetX: -0.25 }
];
function fitSlideArtwork() {
  const viewport = document.querySelector('.home-visual-slide');
  const width = window.innerWidth, height = window.innerHeight;
  if (!width || !height) return;
  for (const slide of slideArtwork) {
    const img = document.querySelector(slide.selector);
    if (!img) continue;
    // Prioridade: a ponta amarela alcança a borda horizontal.
    // Não aumentar além do necessário para a leitura do desenho.
    const horizontal = slide.side === 'right' ? width / slide.tip : width / (slide.viewWidth - slide.tip);
    const vertical = height / slide.viewHeight;
    const scale = Math.max(horizontal, vertical);
    const actualWidth = slide.viewWidth * scale;
    const actualHeight = slide.viewHeight * scale;
    const baseLeft = slide.side === 'right' ? width - slide.tip * scale : -slide.tip * scale;
    const left = baseLeft - width * (slide.offsetX || 0);
    img.style.width = `${actualWidth}px`;
    img.style.height = `${actualHeight}px`;
    img.style.left = `${left}px`;
    img.style.top = `${(height - actualHeight) / 2}px`;
  }
}
window.addEventListener('resize', fitSlideArtwork, { passive: true });
fitSlideArtwork();
